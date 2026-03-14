from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import AppAuthSettings


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


def require_password(value: str | None) -> str:
    if not value:
        raise HTTPException(status_code=400, detail="password is required")
    return value


__all__ = ["get_or_create_app_auth_settings", "require_password"]
