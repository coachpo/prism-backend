from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import AppAuthSettings


@dataclass(frozen=True, slots=True)
class AppAuthSettingsSnapshot:
    id: int
    auth_enabled: bool
    username: str | None
    token_version: int


_app_auth_settings_snapshot_cache: AppAuthSettingsSnapshot | None = None


def _build_app_auth_settings_snapshot(
    settings_row: AppAuthSettings,
) -> AppAuthSettingsSnapshot:
    return AppAuthSettingsSnapshot(
        id=settings_row.id,
        auth_enabled=settings_row.auth_enabled,
        username=settings_row.username,
        token_version=settings_row.token_version,
    )


def invalidate_app_auth_settings_snapshot_cache() -> None:
    global _app_auth_settings_snapshot_cache
    _app_auth_settings_snapshot_cache = None


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


async def get_app_auth_settings_snapshot(
    db: AsyncSession,
) -> AppAuthSettingsSnapshot:
    global _app_auth_settings_snapshot_cache

    if _app_auth_settings_snapshot_cache is not None:
        return _app_auth_settings_snapshot_cache

    settings_row = await get_or_create_app_auth_settings(db)
    _app_auth_settings_snapshot_cache = _build_app_auth_settings_snapshot(settings_row)
    return _app_auth_settings_snapshot_cache


def require_password(value: str | None) -> str:
    if not value:
        raise HTTPException(status_code=400, detail="password is required")
    return value


__all__ = [
    "AppAuthSettingsSnapshot",
    "get_app_auth_settings_snapshot",
    "get_or_create_app_auth_settings",
    "invalidate_app_auth_settings_snapshot_cache",
    "require_password",
]
