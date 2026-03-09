from __future__ import annotations

import smtplib
import logging
from datetime import timedelta
from email.message import EmailMessage

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    build_proxy_api_key,
    build_refresh_token_record,
    create_access_token,
    parse_proxy_api_key,
)
from app.core.config import get_settings
from app.core.crypto import (
    hash_opaque_token,
    hash_password,
    generate_otp_code,
    verify_opaque_token,
    verify_password,
)
from app.core.time import utc_now
from app.models.models import (
    AppAuthSettings,
    PasswordResetChallenge,
    ProxyApiKey,
    RefreshToken,
)
from app.schemas.schemas import AuthSettingsResponse, ProxyApiKeyResponse

PROXY_KEY_LIMIT = 10
logger = logging.getLogger(__name__)


async def get_or_create_app_auth_settings(db: AsyncSession) -> AppAuthSettings:
    settings_row = (
        await db.execute(
            select(AppAuthSettings)
            .where(AppAuthSettings.singleton_key == "app")
            .order_by(AppAuthSettings.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if settings_row is None:
        settings_row = AppAuthSettings(singleton_key="app", auth_enabled=False)
        db.add(settings_row)
        await db.flush()
    return settings_row


def build_auth_settings_response(settings_row: AppAuthSettings) -> AuthSettingsResponse:
    return AuthSettingsResponse(
        auth_enabled=settings_row.auth_enabled,
        username=settings_row.username,
        email=settings_row.email,
        email_bound_at=settings_row.email_bound_at,
        pending_email=settings_row.pending_email,
        email_verification_required=bool(settings_row.pending_email),
        has_password=bool(settings_row.password_hash),
        proxy_key_limit=PROXY_KEY_LIMIT,
    )


def require_password(value: str | None) -> str:
    if not value:
        raise HTTPException(status_code=400, detail="password is required")
    return value


async def update_auth_settings(
    db: AsyncSession,
    *,
    settings_row: AppAuthSettings,
    auth_enabled: bool,
    username: str | None,
    password: str | None,
) -> AppAuthSettings:
    revoke_sessions = False
    if auth_enabled:
        if not username:
            raise HTTPException(status_code=400, detail="username is required")
        if not settings_row.email or settings_row.email_bound_at is None:
            raise HTTPException(status_code=400, detail="A verified email is required")
        if not settings_row.password_hash and not password:
            raise HTTPException(status_code=400, detail="password is required")
        settings_row.username = username
        if password:
            settings_row.password_hash = hash_password(password)
            settings_row.token_version += 1
            revoke_sessions = True
        settings_row.auth_enabled = True
    else:
        if settings_row.auth_enabled:
            settings_row.token_version += 1
            revoke_sessions = True
        settings_row.auth_enabled = False
        if username:
            settings_row.username = username
        if password:
            settings_row.password_hash = hash_password(password)
            settings_row.token_version += 1
            revoke_sessions = True
    settings_row.updated_at = utc_now()
    if revoke_sessions:
        await revoke_all_refresh_tokens(db, auth_subject_id=settings_row.id)
    await db.flush()
    return settings_row


async def begin_email_verification(
    db: AsyncSession, *, settings_row: AppAuthSettings, email: str
) -> tuple[AppAuthSettings, str]:
    otp_code = generate_otp_code()
    settings_row.pending_email = email
    settings_row.email_verification_code_hash = hash_opaque_token(otp_code)
    settings_row.email_verification_expires_at = utc_now() + timedelta(
        seconds=get_settings().auth_reset_code_ttl_seconds
    )
    settings_row.email_verification_attempt_count = 0
    settings_row.updated_at = utc_now()
    await db.flush()
    return settings_row, otp_code


async def confirm_email_verification(
    db: AsyncSession, *, settings_row: AppAuthSettings, otp_code: str
) -> AppAuthSettings:
    if (
        not settings_row.pending_email
        or not settings_row.email_verification_code_hash
        or settings_row.email_verification_expires_at is None
        or settings_row.email_verification_expires_at < utc_now()
    ):
        raise HTTPException(
            status_code=400, detail="Email verification code is invalid or expired"
        )
    if settings_row.email_verification_attempt_count >= 5:
        raise HTTPException(status_code=429, detail="Too many verification attempts")

    settings_row.email_verification_attempt_count += 1
    if not verify_opaque_token(otp_code, settings_row.email_verification_code_hash):
        await db.flush()
        raise HTTPException(
            status_code=400, detail="Email verification code is invalid or expired"
        )

    settings_row.email = settings_row.pending_email
    settings_row.email_bound_at = utc_now()
    settings_row.pending_email = None
    settings_row.email_verification_code_hash = None
    settings_row.email_verification_expires_at = None
    settings_row.email_verification_attempt_count = 0
    settings_row.updated_at = utc_now()
    await db.flush()
    return settings_row


async def authenticate_user(
    db: AsyncSession,
    *,
    username: str,
    password: str,
    user_agent: str | None,
    ip_address: str | None,
) -> tuple[AppAuthSettings, str, str]:
    settings_row = await get_or_create_app_auth_settings(db)
    if not settings_row.auth_enabled:
        raise HTTPException(status_code=400, detail="Authentication is not enabled")
    if settings_row.username != username or not settings_row.password_hash:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not verify_password(password, settings_row.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    access_token = create_access_token(
        subject_id=settings_row.id,
        username=settings_row.username or "",
        token_version=settings_row.token_version,
    )
    raw_refresh_token, refresh_hash, expires_at = build_refresh_token_record()
    db.add(
        RefreshToken(
            auth_subject_id=settings_row.id,
            token_hash=refresh_hash,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
    )
    settings_row.last_login_at = utc_now()
    await db.flush()
    return settings_row, access_token, raw_refresh_token


async def rotate_refresh_token(
    db: AsyncSession,
    *,
    raw_refresh_token: str,
    user_agent: str | None,
    ip_address: str | None,
) -> tuple[AppAuthSettings, str, str]:
    refresh_hash = hash_opaque_token(raw_refresh_token)
    refresh_row = (
        await db.execute(
            select(RefreshToken)
            .where(RefreshToken.token_hash == refresh_hash)
            .order_by(RefreshToken.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if (
        refresh_row is None
        or refresh_row.revoked_at is not None
        or refresh_row.expires_at < utc_now()
    ):
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    settings_row = await get_or_create_app_auth_settings(db)
    if refresh_row.auth_subject_id != settings_row.id or not settings_row.auth_enabled:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    access_token = create_access_token(
        subject_id=settings_row.id,
        username=settings_row.username or "",
        token_version=settings_row.token_version,
    )
    new_raw_refresh_token, new_refresh_hash, expires_at = build_refresh_token_record()
    refresh_row.revoked_at = utc_now()
    refresh_row.last_used_at = utc_now()
    db.add(
        RefreshToken(
            auth_subject_id=settings_row.id,
            token_hash=new_refresh_hash,
            expires_at=expires_at,
            rotated_from_id=refresh_row.id,
            user_agent=user_agent,
            ip_address=ip_address,
        )
    )
    await db.flush()
    return settings_row, access_token, new_raw_refresh_token


async def revoke_all_refresh_tokens(db: AsyncSession, *, auth_subject_id: int) -> None:
    await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.auth_subject_id == auth_subject_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=utc_now())
    )


async def revoke_refresh_token(db: AsyncSession, *, raw_refresh_token: str) -> None:
    refresh_hash = hash_opaque_token(raw_refresh_token)
    await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.token_hash == refresh_hash, RefreshToken.revoked_at.is_(None)
        )
        .values(revoked_at=utc_now())
    )


async def list_proxy_api_keys(db: AsyncSession) -> list[ProxyApiKey]:
    result = await db.execute(select(ProxyApiKey).order_by(ProxyApiKey.id.asc()))
    return list(result.scalars().all())


async def create_proxy_api_key(
    db: AsyncSession, *, name: str, notes: str | None, auth_subject_id: int | None
) -> tuple[str, ProxyApiKey]:
    count = await db.execute(select(func.count(ProxyApiKey.id)))
    if count.scalar_one() >= PROXY_KEY_LIMIT:
        raise HTTPException(status_code=409, detail="Maximum 10 proxy API keys reached")
    for _ in range(5):
        raw_key, key_prefix, last_four = build_proxy_api_key()
        row = ProxyApiKey(
            name=name,
            key_prefix=key_prefix,
            key_hash=hash_opaque_token(raw_key),
            last_four=last_four,
            is_active=True,
            created_by_auth_subject_id=auth_subject_id,
            notes=notes,
        )
        db.add(row)
        try:
            await db.flush()
            return raw_key, row
        except IntegrityError as exc:
            await db.rollback()
            if "uq_proxy_api_keys_prefix" not in str(exc):
                raise
    raise HTTPException(
        status_code=500, detail="Failed to generate a unique proxy API key"
    )


async def rotate_proxy_api_key(
    db: AsyncSession, *, key_id: int
) -> tuple[str, ProxyApiKey]:
    row = (
        await db.execute(select(ProxyApiKey).where(ProxyApiKey.id == key_id).limit(1))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Proxy API key not found")
    for _ in range(5):
        raw_key, key_prefix, last_four = build_proxy_api_key()
        row.key_prefix = key_prefix
        row.key_hash = hash_opaque_token(raw_key)
        row.last_four = last_four
        row.updated_at = utc_now()
        try:
            await db.flush()
            return raw_key, row
        except IntegrityError as exc:
            await db.rollback()
            refreshed = (
                await db.execute(
                    select(ProxyApiKey).where(ProxyApiKey.id == key_id).limit(1)
                )
            ).scalar_one_or_none()
            if refreshed is None:
                raise HTTPException(status_code=404, detail="Proxy API key not found")
            row = refreshed
            if "uq_proxy_api_keys_prefix" not in str(exc):
                raise
    raise HTTPException(status_code=500, detail="Failed to rotate proxy API key")


async def delete_proxy_api_key(db: AsyncSession, *, key_id: int) -> None:
    row = (
        await db.execute(select(ProxyApiKey).where(ProxyApiKey.id == key_id).limit(1))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Proxy API key not found")
    await db.delete(row)
    await db.flush()


async def verify_proxy_api_key(db: AsyncSession, *, raw_key: str) -> ProxyApiKey | None:
    try:
        normalized_key, key_prefix = parse_proxy_api_key(raw_key)
    except ValueError:
        return None
    row = (
        await db.execute(
            select(ProxyApiKey)
            .where(ProxyApiKey.key_prefix == key_prefix)
            .order_by(ProxyApiKey.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None or not row.is_active:
        return None
    if row.expires_at is not None and row.expires_at < utc_now():
        return None
    if not verify_opaque_token(normalized_key, row.key_hash):
        return None
    row.last_used_at = utc_now()
    return row


async def create_password_reset_challenge(
    db: AsyncSession, *, settings_row: AppAuthSettings, requested_ip: str | None
) -> tuple[PasswordResetChallenge, str]:
    otp_code = generate_otp_code()
    expires_at = utc_now() + timedelta(
        seconds=get_settings().auth_reset_code_ttl_seconds
    )
    await db.execute(
        update(PasswordResetChallenge)
        .where(
            PasswordResetChallenge.auth_subject_id == settings_row.id,
            PasswordResetChallenge.consumed_at.is_(None),
        )
        .values(consumed_at=utc_now())
    )
    challenge = PasswordResetChallenge(
        auth_subject_id=settings_row.id,
        otp_hash=hash_opaque_token(otp_code),
        expires_at=expires_at,
        requested_ip=requested_ip,
    )
    db.add(challenge)
    await db.flush()
    return challenge, otp_code


def send_password_reset_email(*, recipient: str, otp_code: str) -> None:
    settings = get_settings()
    if not settings.smtp_host or not settings.smtp_sender_email:
        raise HTTPException(status_code=503, detail="SMTP is not configured")
    message = EmailMessage()
    message["Subject"] = "Prism password reset code"
    message["From"] = (
        f"{settings.smtp_sender_name} <{settings.smtp_sender_email}>"
        if settings.smtp_sender_name
        else settings.smtp_sender_email
    )
    message["To"] = recipient
    message.set_content(
        "Use this Prism password reset code to continue: "
        f"{otp_code}. The code expires in {get_settings().auth_reset_code_ttl_seconds // 60} minutes."
    )
    if settings.log_level.lower() == "debug":
        logger.debug("Prism password reset OTP for %s: %s", recipient, otp_code)
    smtp_username = settings.smtp_username
    smtp_password = settings.smtp_password
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if smtp_username and smtp_password:
            smtp.login(smtp_username, smtp_password)
        smtp.send_message(message)


def send_email_verification_otp(*, recipient: str, otp_code: str) -> None:
    settings = get_settings()
    if not settings.smtp_host or not settings.smtp_sender_email:
        raise HTTPException(status_code=503, detail="SMTP is not configured")
    message = EmailMessage()
    message["Subject"] = "Prism email verification code"
    message["From"] = (
        f"{settings.smtp_sender_name} <{settings.smtp_sender_email}>"
        if settings.smtp_sender_name
        else settings.smtp_sender_email
    )
    message["To"] = recipient
    message.set_content(
        "Use this Prism verification code to bind your email: "
        f"{otp_code}. The code expires in {get_settings().auth_reset_code_ttl_seconds // 60} minutes."
    )
    if settings.log_level.lower() == "debug":
        logger.debug("Prism email verification OTP for %s: %s", recipient, otp_code)
    smtp_username = settings.smtp_username
    smtp_password = settings.smtp_password
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if smtp_username and smtp_password:
            smtp.login(smtp_username, smtp_password)
        smtp.send_message(message)


async def consume_password_reset_challenge(
    db: AsyncSession, *, otp_code: str, new_password: str
) -> AppAuthSettings:
    settings_row = await get_or_create_app_auth_settings(db)
    challenge = (
        await db.execute(
            select(PasswordResetChallenge)
            .where(
                PasswordResetChallenge.auth_subject_id == settings_row.id,
                PasswordResetChallenge.consumed_at.is_(None),
            )
            .order_by(PasswordResetChallenge.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if challenge is None or challenge.expires_at < utc_now():
        raise HTTPException(status_code=400, detail="Reset code is invalid or expired")
    if challenge.attempt_count >= 5:
        raise HTTPException(status_code=429, detail="Too many reset attempts")
    challenge.attempt_count += 1
    if not verify_opaque_token(otp_code, challenge.otp_hash):
        await db.flush()
        raise HTTPException(status_code=400, detail="Reset code is invalid or expired")
    challenge.consumed_at = utc_now()
    settings_row.password_hash = hash_password(new_password)
    settings_row.token_version += 1
    settings_row.must_change_password = False
    settings_row.updated_at = utc_now()
    await revoke_all_refresh_tokens(db, auth_subject_id=settings_row.id)
    await db.flush()
    return settings_row


def serialize_proxy_api_key(row: ProxyApiKey) -> ProxyApiKeyResponse:
    return ProxyApiKeyResponse(
        id=row.id,
        name=row.name,
        key_prefix=row.key_prefix,
        key_preview=f"{row.key_prefix}{'•' * 8}{row.last_four}",
        is_active=row.is_active,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        last_used_ip=row.last_used_ip,
        notes=row.notes,
        rotated_from_id=row.rotated_from_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
