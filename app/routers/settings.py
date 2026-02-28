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
 )

router = APIRouter(prefix="/api/settings", tags=["settings"])

async def _get_or_create_user_settings(
    db: AsyncSession,
    *,
    profile_id: int,
 ) -> UserSetting:
    settings_row = (
        await db.execute(
            select(UserSetting)
            .where(UserSetting.profile_id == profile_id)
            .order_by(UserSetting.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if settings_row is None:
        settings_row = UserSetting(
            profile_id=profile_id,
            report_currency_code="USD",
            report_currency_symbol="$",
            timezone_preference=None,
        )
        db.add(settings_row)
        await db.flush()
    return settings_row


@router.get("/costing", response_model=CostingSettingsResponse)
async def get_costing_settings(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    settings_row = await _get_or_create_user_settings(db, profile_id=profile_id)

    fx_rows = (
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

    return CostingSettingsResponse(
        profile_id=profile_id,
        report_currency_code=settings_row.report_currency_code,
        report_currency_symbol=settings_row.report_currency_symbol,
        timezone_preference=settings_row.timezone_preference,
        endpoint_fx_mappings=[
            EndpointFxMapping(
                model_id=row.model_id,
                endpoint_id=row.endpoint_id,
                fx_rate=row.fx_rate,
            )
            for row in fx_rows
        ],
    )


@router.put("/costing", response_model=CostingSettingsResponse)
async def update_costing_settings(
    body: CostingSettingsUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    settings_row = await _get_or_create_user_settings(db, profile_id=profile_id)

    endpoint_ids = sorted({item.endpoint_id for item in body.endpoint_fx_mappings})
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

    for mapping in body.endpoint_fx_mappings:
        if (mapping.model_id, mapping.endpoint_id) not in valid_pairs:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No connection found for "
                    f"model_id='{mapping.model_id}' and endpoint_id={mapping.endpoint_id}"
                ),
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

    return CostingSettingsResponse(
        profile_id=profile_id,
        report_currency_code=settings_row.report_currency_code,
        report_currency_symbol=settings_row.report_currency_symbol,
        timezone_preference=settings_row.timezone_preference,
        endpoint_fx_mappings=body.endpoint_fx_mappings,
    )
