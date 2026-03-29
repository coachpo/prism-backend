from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_effective_profile_id
from app.schemas.schemas import MonitoringSettingsResponse, MonitoringSettingsUpdate

from .helpers import get_or_create_user_settings

MIN_MONITORING_PROBE_INTERVAL_SECONDS = 30
MAX_MONITORING_PROBE_INTERVAL_SECONDS = 3_600
DEFAULT_MONITORING_PROBE_INTERVAL_SECONDS = 300

router = APIRouter()


def _clamp_monitoring_probe_interval_seconds(value: int) -> int:
    return max(
        MIN_MONITORING_PROBE_INTERVAL_SECONDS,
        min(MAX_MONITORING_PROBE_INTERVAL_SECONDS, value),
    )


def _resolve_monitoring_probe_interval_seconds(value: int | None) -> int:
    if value is None:
        return DEFAULT_MONITORING_PROBE_INTERVAL_SECONDS
    return _clamp_monitoring_probe_interval_seconds(value)


def _build_monitoring_settings_response(
    *,
    profile_id: int,
    monitoring_probe_interval_seconds: int,
) -> MonitoringSettingsResponse:
    return MonitoringSettingsResponse(
        profile_id=profile_id,
        monitoring_probe_interval_seconds=monitoring_probe_interval_seconds,
    )


@router.get("/monitoring", response_model=MonitoringSettingsResponse)
async def get_monitoring_settings(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    settings_row = await get_or_create_user_settings(db, profile_id=profile_id)
    return _build_monitoring_settings_response(
        profile_id=profile_id,
        monitoring_probe_interval_seconds=_resolve_monitoring_probe_interval_seconds(
            settings_row.monitoring_probe_interval_seconds
        ),
    )


@router.put("/monitoring", response_model=MonitoringSettingsResponse)
async def update_monitoring_settings(
    body: MonitoringSettingsUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    settings_row = await get_or_create_user_settings(db, profile_id=profile_id)
    settings_row.monitoring_probe_interval_seconds = (
        _clamp_monitoring_probe_interval_seconds(body.monitoring_probe_interval_seconds)
    )
    await db.flush()
    return _build_monitoring_settings_response(
        profile_id=profile_id,
        monitoring_probe_interval_seconds=settings_row.monitoring_probe_interval_seconds,
    )


__all__ = [
    "DEFAULT_MONITORING_PROBE_INTERVAL_SECONDS",
    "MAX_MONITORING_PROBE_INTERVAL_SECONDS",
    "MIN_MONITORING_PROBE_INTERVAL_SECONDS",
    "get_monitoring_settings",
    "router",
    "update_monitoring_settings",
]
