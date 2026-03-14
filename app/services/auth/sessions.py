from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    RefreshSessionDuration,
    build_refresh_token_record,
    create_access_token,
    get_refresh_token_expiry,
    normalize_refresh_session_duration,
)
from app.core.crypto import hash_opaque_token, verify_password
from app.core.time import utc_now
from app.models.models import AppAuthSettings, RefreshToken

from .app_settings import get_or_create_app_auth_settings


def _build_session_tokens(
    *,
    settings_row: AppAuthSettings,
    session_duration: RefreshSessionDuration,
) -> tuple[str, str, str, datetime]:
    access_token = create_access_token(
        subject_id=settings_row.id,
        username=settings_row.username or "",
        token_version=settings_row.token_version,
    )
    expires_at = get_refresh_token_expiry(session_duration=session_duration)
    raw_refresh_token, refresh_hash, expires_at = build_refresh_token_record(
        expires_at=expires_at
    )
    return access_token, raw_refresh_token, refresh_hash, expires_at


async def authenticate_user(
    db: AsyncSession,
    *,
    username: str,
    password: str,
    session_duration: RefreshSessionDuration,
    user_agent: str | None,
    ip_address: str | None,
) -> tuple[AppAuthSettings, str, str, datetime, RefreshSessionDuration]:
    settings_row = await get_or_create_app_auth_settings(db)
    if not settings_row.auth_enabled:
        raise HTTPException(status_code=400, detail="Authentication is not enabled")
    if settings_row.username != username or not settings_row.password_hash:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not verify_password(password, settings_row.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    access_token, raw_refresh_token, refresh_hash, expires_at = _build_session_tokens(
        settings_row=settings_row,
        session_duration=session_duration,
    )
    db.add(
        RefreshToken(
            auth_subject_id=settings_row.id,
            token_hash=refresh_hash,
            session_duration=session_duration,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
    )
    settings_row.last_login_at = utc_now()
    await db.flush()
    return settings_row, access_token, raw_refresh_token, expires_at, session_duration


async def create_session_for_auth_subject(
    db: AsyncSession,
    *,
    auth_subject_id: int,
    session_duration: RefreshSessionDuration,
    user_agent: str | None,
    ip_address: str | None,
) -> tuple[AppAuthSettings, str, str, datetime, RefreshSessionDuration]:
    settings_row = await get_or_create_app_auth_settings(db)
    if not settings_row.auth_enabled or settings_row.id != auth_subject_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    access_token, raw_refresh_token, refresh_hash, expires_at = _build_session_tokens(
        settings_row=settings_row,
        session_duration=session_duration,
    )
    db.add(
        RefreshToken(
            auth_subject_id=settings_row.id,
            token_hash=refresh_hash,
            session_duration=session_duration,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
    )
    settings_row.last_login_at = utc_now()
    await db.flush()
    return settings_row, access_token, raw_refresh_token, expires_at, session_duration


async def rotate_refresh_token(
    db: AsyncSession,
    *,
    raw_refresh_token: str,
    user_agent: str | None,
    ip_address: str | None,
) -> tuple[AppAuthSettings, str, str, datetime, RefreshSessionDuration]:
    refresh_hash = hash_opaque_token(raw_refresh_token)
    refresh_row = (
        await db.execute(
            select(RefreshToken)
            .where(RefreshToken.token_hash == refresh_hash)
            .order_by(RefreshToken.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if refresh_row is None or refresh_row.expires_at < utc_now():
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    if refresh_row.revoked_at is not None:
        await revoke_refresh_token_family(db, refresh_token_id=refresh_row.id)
        await db.flush()
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    settings_row = await get_or_create_app_auth_settings(db)
    if refresh_row.auth_subject_id != settings_row.id or not settings_row.auth_enabled:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    try:
        session_duration: RefreshSessionDuration = normalize_refresh_session_duration(
            refresh_row.session_duration
        )
    except ValueError as exc:
        refresh_row.revoked_at = utc_now()
        await db.flush()
        raise HTTPException(status_code=401, detail="Invalid refresh token") from exc

    access_token = create_access_token(
        subject_id=settings_row.id,
        username=settings_row.username or "",
        token_version=settings_row.token_version,
    )
    new_raw_refresh_token, new_refresh_hash, expires_at = build_refresh_token_record(
        expires_at=refresh_row.expires_at
    )
    refresh_row.revoked_at = utc_now()
    refresh_row.last_used_at = utc_now()
    db.add(
        RefreshToken(
            auth_subject_id=settings_row.id,
            token_hash=new_refresh_hash,
            session_duration=session_duration,
            expires_at=expires_at,
            rotated_from_id=refresh_row.id,
            user_agent=user_agent,
            ip_address=ip_address,
        )
    )
    await db.flush()
    return (
        settings_row,
        access_token,
        new_raw_refresh_token,
        expires_at,
        session_duration,
    )


async def revoke_all_refresh_tokens(db: AsyncSession, *, auth_subject_id: int) -> None:
    await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.auth_subject_id == auth_subject_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=utc_now())
    )


async def revoke_refresh_token_family(
    db: AsyncSession, *, refresh_token_id: int
) -> None:
    family_root_id = refresh_token_id
    current_parent_id = (
        await db.execute(
            select(RefreshToken.rotated_from_id)
            .where(RefreshToken.id == refresh_token_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    while current_parent_id is not None:
        family_root_id = current_parent_id
        current_parent_id = (
            await db.execute(
                select(RefreshToken.rotated_from_id)
                .where(RefreshToken.id == family_root_id)
                .limit(1)
            )
        ).scalar_one_or_none()

    family_ids = {family_root_id}
    frontier = [family_root_id]
    while frontier:
        child_ids = list(
            (
                await db.execute(
                    select(RefreshToken.id)
                    .where(RefreshToken.rotated_from_id.in_(frontier))
                    .order_by(RefreshToken.id.asc())
                )
            )
            .scalars()
            .all()
        )
        next_frontier = [
            child_id for child_id in child_ids if child_id not in family_ids
        ]
        if not next_frontier:
            break
        family_ids.update(next_frontier)
        frontier = next_frontier

    await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.id.in_(family_ids),
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=utc_now())
    )


async def revoke_refresh_token(db: AsyncSession, *, raw_refresh_token: str) -> None:
    refresh_hash = hash_opaque_token(raw_refresh_token)
    await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.token_hash == refresh_hash,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=utc_now())
    )


__all__ = [
    "authenticate_user",
    "create_session_for_auth_subject",
    "revoke_all_refresh_tokens",
    "revoke_refresh_token",
    "revoke_refresh_token_family",
    "rotate_refresh_token",
]
