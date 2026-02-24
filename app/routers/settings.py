from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_db
from app.models.models import Endpoint, EndpointFxRateSetting, UserSetting
from app.schemas.schemas import (
    CostingSettingsResponse,
    CostingSettingsUpdate,
    EndpointFxMapping,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])


async def _get_or_create_user_settings(db: AsyncSession) -> UserSetting:
    settings_row = (
        await db.execute(select(UserSetting).order_by(UserSetting.id.asc()).limit(1))
    ).scalar_one_or_none()
    if settings_row is None:
        settings_row = UserSetting(
            report_currency_code="USD", report_currency_symbol="$"
        )
        db.add(settings_row)
        await db.flush()
    return settings_row


@router.get("/costing", response_model=CostingSettingsResponse)
async def get_costing_settings(db: Annotated[AsyncSession, Depends(get_db)]):
    settings_row = await _get_or_create_user_settings(db)

    fx_rows = (
        (
            await db.execute(
                select(EndpointFxRateSetting).order_by(
                    EndpointFxRateSetting.model_id.asc(),
                    EndpointFxRateSetting.endpoint_id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )

    return CostingSettingsResponse(
        report_currency_code=settings_row.report_currency_code,
        report_currency_symbol=settings_row.report_currency_symbol,
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
):
    settings_row = await _get_or_create_user_settings(db)

    endpoint_ids = [item.endpoint_id for item in body.endpoint_fx_mappings]
    endpoint_model_map: dict[int, str] = {}
    if endpoint_ids:
        endpoint_rows = (
            (
                await db.execute(
                    select(Endpoint)
                    .options(selectinload(Endpoint.model_config_rel))
                    .where(Endpoint.id.in_(endpoint_ids))
                )
            )
            .scalars()
            .all()
        )
        endpoint_model_map = {
            ep.id: ep.model_config_rel.model_id
            for ep in endpoint_rows
            if ep.model_config_rel
        }

    for mapping in body.endpoint_fx_mappings:
        mapped_model_id = endpoint_model_map.get(mapping.endpoint_id)
        if mapped_model_id is None:
            raise HTTPException(
                status_code=400,
                detail=f"Endpoint {mapping.endpoint_id} does not exist",
            )
        if mapped_model_id != mapping.model_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Endpoint {mapping.endpoint_id} belongs to model '{mapped_model_id}', "
                    f"not '{mapping.model_id}'"
                ),
            )

    settings_row.report_currency_code = body.report_currency_code
    settings_row.report_currency_symbol = body.report_currency_symbol

    await db.execute(delete(EndpointFxRateSetting))
    for mapping in body.endpoint_fx_mappings:
        db.add(
            EndpointFxRateSetting(
                model_id=mapping.model_id,
                endpoint_id=mapping.endpoint_id,
                fx_rate=mapping.fx_rate,
            )
        )

    await db.flush()

    return CostingSettingsResponse(
        report_currency_code=settings_row.report_currency_code,
        report_currency_symbol=settings_row.report_currency_symbol,
        endpoint_fx_mappings=body.endpoint_fx_mappings,
    )
