from typing import Literal, cast

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.models import Connection, ModelConfig
from app.schemas.schemas import (
    LoadbalanceStrategySummary,
    ModelConfigListResponse,
    ProviderResponse,
)

MODEL_CONFIG_DETAIL_OPTIONS = (
    selectinload(ModelConfig.provider),
    selectinload(ModelConfig.loadbalance_strategy),
    selectinload(ModelConfig.connections).selectinload(Connection.endpoint_rel),
    selectinload(ModelConfig.connections).selectinload(Connection.pricing_template_rel),
)


def build_model_list_response(
    config: ModelConfig,
    *,
    stats: dict[str, object],
    connection_count: int | None = None,
    active_connection_count: int | None = None,
) -> ModelConfigListResponse:
    resolved_connection_count = (
        len(config.connections) if connection_count is None else connection_count
    )
    resolved_active_connection_count = (
        sum(1 for connection in config.connections if connection.is_active)
        if active_connection_count is None
        else active_connection_count
    )
    return ModelConfigListResponse(
        id=config.id,
        profile_id=config.profile_id,
        provider_id=config.provider_id,
        provider=ProviderResponse.model_validate(config.provider),
        model_id=config.model_id,
        display_name=config.display_name,
        model_type=cast(Literal["native", "proxy"], config.model_type),
        redirect_to=config.redirect_to,
        loadbalance_strategy_id=config.loadbalance_strategy_id,
        loadbalance_strategy=(
            LoadbalanceStrategySummary.model_validate(config.loadbalance_strategy)
            if config.loadbalance_strategy is not None
            else None
        ),
        is_enabled=config.is_enabled,
        connection_count=resolved_connection_count,
        active_connection_count=resolved_active_connection_count,
        health_success_rate=cast(float | None, stats.get("health_success_rate")),
        health_total_requests=cast(int, stats.get("health_total_requests", 0)),
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


async def ensure_model_id_available(
    db: AsyncSession,
    *,
    profile_id: int,
    model_id: str,
    exclude_id: int | None = None,
) -> None:
    query = select(ModelConfig).where(
        ModelConfig.profile_id == profile_id,
        ModelConfig.model_id == model_id,
    )
    if exclude_id is not None:
        query = query.where(ModelConfig.id != exclude_id)
    existing = (await db.execute(query)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409, detail=f"Model ID '{model_id}' already exists"
        )


async def load_model_config_detail_or_404(
    db: AsyncSession,
    *,
    model_config_id: int,
    profile_id: int,
) -> ModelConfig:
    result = await db.execute(
        select(ModelConfig)
        .options(*MODEL_CONFIG_DETAIL_OPTIONS)
        .where(
            ModelConfig.id == model_config_id,
            ModelConfig.profile_id == profile_id,
        )
    )
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail="Model configuration not found")
    return config


async def list_proxy_referrers(
    db: AsyncSession,
    *,
    profile_id: int,
    model_id: str,
    exclude_id: int | None = None,
) -> list[ModelConfig]:
    query = select(ModelConfig).where(
        ModelConfig.profile_id == profile_id,
        ModelConfig.redirect_to == model_id,
    )
    if exclude_id is not None:
        query = query.where(ModelConfig.id != exclude_id)
    return list((await db.execute(query)).scalars().all())


async def validate_proxy_model(
    db: AsyncSession,
    *,
    profile_id: int,
    model_type: str,
    redirect_to: str | None,
    provider_id: int,
    exclude_model_id: str | None = None,
) -> None:
    if model_type == "proxy":
        if not redirect_to:
            raise HTTPException(
                status_code=400,
                detail="redirect_to is required for proxy models",
            )
        if exclude_model_id is not None and redirect_to == exclude_model_id:
            raise HTTPException(
                status_code=400,
                detail="Proxy model cannot redirect to itself",
            )
        target_result = await db.execute(
            select(ModelConfig)
            .options(selectinload(ModelConfig.provider))
            .where(
                ModelConfig.profile_id == profile_id,
                ModelConfig.model_id == redirect_to,
            )
        )
        target = target_result.scalar_one_or_none()
        if not target:
            raise HTTPException(
                status_code=400,
                detail=f"Target model '{redirect_to}' not found",
            )
        if target.model_type != "native":
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Target model '{redirect_to}' is not a native model "
                    "(chained proxies not allowed)"
                ),
            )
        if target.provider_id != provider_id:
            raise HTTPException(
                status_code=400,
                detail="Proxy target must be the same provider as the proxy model",
            )
    elif model_type == "native" and redirect_to:
        raise HTTPException(
            status_code=400,
            detail="redirect_to must be null for native models",
        )


__all__ = [
    "MODEL_CONFIG_DETAIL_OPTIONS",
    "build_model_list_response",
    "ensure_model_id_available",
    "list_proxy_referrers",
    "load_model_config_detail_or_404",
    "validate_proxy_model",
]
