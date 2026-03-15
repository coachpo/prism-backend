from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_effective_profile_id
from app.models.models import (
    Connection,
    EndpointFxRateSetting,
    ModelConfig,
    UserSetting,
)
from app.schemas.schemas import (
    CostingSettingsResponse,
    CostingSettingsUpdate,
    EndpointFxMapping,
    TimezonePreferenceResponse,
    TimezonePreferenceUpdate,
)

from .helpers import get_or_create_user_settings

router = APIRouter()


def _build_costing_settings_response(
    *,
    settings_row: UserSetting,
    profile_id: int,
    endpoint_fx_mappings: list[EndpointFxMapping],
) -> CostingSettingsResponse:
    return CostingSettingsResponse(
        profile_id=profile_id,
        report_currency_code=settings_row.report_currency_code,
        report_currency_symbol=settings_row.report_currency_symbol,
        timezone_preference=settings_row.timezone_preference,
        endpoint_fx_mappings=endpoint_fx_mappings,
    )


def _build_timezone_preference_response(
    *, settings_row: UserSetting, profile_id: int
) -> TimezonePreferenceResponse:
    return TimezonePreferenceResponse(
        profile_id=profile_id,
        timezone_preference=settings_row.timezone_preference,
    )


async def _list_endpoint_fx_mappings(
    db: AsyncSession,
    *,
    profile_id: int,
) -> list[EndpointFxMapping]:
    rows = (
        (
            await db.execute(
                select(EndpointFxRateSetting)
                .where(EndpointFxRateSetting.profile_id == profile_id)
                .order_by(
                    EndpointFxRateSetting.model_id.asc(),
                    EndpointFxRateSetting.endpoint_id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        EndpointFxMapping(
            model_id=row.model_id,
            endpoint_id=row.endpoint_id,
            fx_rate=row.fx_rate,
        )
        for row in rows
    ]


async def _validate_endpoint_fx_mappings(
    db: AsyncSession,
    *,
    profile_id: int,
    endpoint_fx_mappings: list[EndpointFxMapping],
) -> None:
    endpoint_ids = sorted({item.endpoint_id for item in endpoint_fx_mappings})
    valid_pairs: set[tuple[str, int]] = set()
    if endpoint_ids:
        rows = (
            await db.execute(
                select(ModelConfig.model_id, Connection.endpoint_id)
                .join(Connection, Connection.model_config_id == ModelConfig.id)
                .where(
                    Connection.profile_id == profile_id,
                    ModelConfig.profile_id == profile_id,
                    Connection.endpoint_id.in_(endpoint_ids),
                )
            )
        ).all()
        valid_pairs = {(row.model_id, row.endpoint_id) for row in rows}

    for mapping in endpoint_fx_mappings:
        if (mapping.model_id, mapping.endpoint_id) not in valid_pairs:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No connection found for "
                    f"model_id='{mapping.model_id}' and endpoint_id={mapping.endpoint_id}"
                ),
            )


@router.get("/costing", response_model=CostingSettingsResponse)
async def get_costing_settings(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    settings_row = await get_or_create_user_settings(db, profile_id=profile_id)
    endpoint_fx_mappings = await _list_endpoint_fx_mappings(db, profile_id=profile_id)
    return _build_costing_settings_response(
        settings_row=settings_row,
        profile_id=profile_id,
        endpoint_fx_mappings=endpoint_fx_mappings,
    )


@router.get("/timezone", response_model=TimezonePreferenceResponse)
async def get_timezone_preference(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    settings_row = await get_or_create_user_settings(db, profile_id=profile_id)
    return _build_timezone_preference_response(
        settings_row=settings_row,
        profile_id=profile_id,
    )


@router.put("/costing", response_model=CostingSettingsResponse)
async def update_costing_settings(
    body: CostingSettingsUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    settings_row = await get_or_create_user_settings(db, profile_id=profile_id)
    await _validate_endpoint_fx_mappings(
        db,
        profile_id=profile_id,
        endpoint_fx_mappings=body.endpoint_fx_mappings,
    )

    settings_row.report_currency_code = body.report_currency_code
    settings_row.report_currency_symbol = body.report_currency_symbol
    settings_row.timezone_preference = body.timezone_preference

    await db.execute(
        delete(EndpointFxRateSetting).where(
            EndpointFxRateSetting.profile_id == profile_id,
        )
    )
    for mapping in body.endpoint_fx_mappings:
        db.add(
            EndpointFxRateSetting(
                profile_id=profile_id,
                model_id=mapping.model_id,
                endpoint_id=mapping.endpoint_id,
                fx_rate=mapping.fx_rate,
            )
        )

    await db.flush()

    return _build_costing_settings_response(
        settings_row=settings_row,
        profile_id=profile_id,
        endpoint_fx_mappings=body.endpoint_fx_mappings,
    )


@router.put("/timezone", response_model=TimezonePreferenceResponse)
async def update_timezone_preference(
    body: TimezonePreferenceUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    settings_row = await get_or_create_user_settings(db, profile_id=profile_id)
    settings_row.timezone_preference = body.timezone_preference
    await db.flush()
    return _build_timezone_preference_response(
        settings_row=settings_row,
        profile_id=profile_id,
    )


__all__ = [
    "get_costing_settings",
    "get_timezone_preference",
    "router",
    "update_costing_settings",
    "update_timezone_preference",
]
