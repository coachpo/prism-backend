from __future__ import annotations

from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.crypto import (
    generate_otp_code,
    hash_opaque_token,
    hash_password,
    verify_opaque_token,
)
from app.core.time import utc_now
from app.models.models import AppAuthSettings, PasswordResetChallenge

from .app_settings import get_or_create_app_auth_settings
from .sessions import revoke_all_refresh_tokens


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


__all__ = ["consume_password_reset_challenge", "create_password_reset_challenge"]
