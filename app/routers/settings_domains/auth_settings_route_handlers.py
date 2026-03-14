from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.schemas import AuthSettingsResponse, AuthSettingsUpdate
from app.services.auth_service import (
    build_auth_settings_response,
    get_or_create_app_auth_settings,
    update_auth_settings,
)

router = APIRouter()


@router.get("/auth", response_model=AuthSettingsResponse)
async def get_auth_settings(
    db: Annotated[AsyncSession, Depends(get_db)],
):
    settings_row = await get_or_create_app_auth_settings(db)
    return build_auth_settings_response(settings_row)


@router.put("/auth", response_model=AuthSettingsResponse)
async def put_auth_settings(
    body: AuthSettingsUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    settings_row = await get_or_create_app_auth_settings(db)
    updated = await update_auth_settings(
        db,
        settings_row=settings_row,
        auth_enabled=body.auth_enabled,
        username=body.username,
        password=body.password,
    )
    return build_auth_settings_response(updated)


__all__ = ["get_auth_settings", "put_auth_settings", "router"]
