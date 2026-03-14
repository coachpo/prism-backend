from __future__ import annotations

from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.crypto import (
    generate_otp_code,
    hash_opaque_token,
    hash_password,
    verify_opaque_token,
)
from app.core.time import utc_now
from app.models.models import AppAuthSettings
from app.schemas.schemas import AuthSettingsResponse

from .proxy_keys import PROXY_KEY_LIMIT
from .sessions import revoke_all_refresh_tokens


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


__all__ = [
    "begin_email_verification",
    "build_auth_settings_response",
    "confirm_email_verification",
    "update_auth_settings",
]
