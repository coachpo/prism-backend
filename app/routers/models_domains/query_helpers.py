from typing import Literal, cast

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.models import Connection, ModelConfig, ModelProxyTarget
from app.schemas.schemas import (
    LoadbalanceStrategySummary,
    ModelConfigListResponse,
    ProxyTargetReference,
    VendorResponse,
)

MODEL_CONFIG_DETAIL_OPTIONS = (
    selectinload(ModelConfig.vendor),
    selectinload(ModelConfig.loadbalance_strategy),
    selectinload(ModelConfig.proxy_targets).selectinload(
        ModelProxyTarget.target_model_config
    ),
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
        vendor_id=config.vendor_id,
        vendor=VendorResponse.model_validate(config.vendor),
        api_family=cast(Literal["openai", "anthropic", "gemini"], config.api_family),
        model_id=config.model_id,
        display_name=config.display_name,
        model_type=cast(Literal["native", "proxy"], config.model_type),
        proxy_targets=[
            ProxyTargetReference.model_validate(proxy_target)
            for proxy_target in sorted(
                config.proxy_targets,
                key=lambda proxy_target: (proxy_target.position, proxy_target.id),
            )
        ],
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
    target_model_id_subquery = (
        select(ModelConfig.id)
        .where(
            ModelConfig.profile_id == profile_id,
            ModelConfig.model_id == model_id,
        )
        .scalar_subquery()
    )
    query = (
        select(ModelConfig)
        .join(
            ModelProxyTarget,
            ModelProxyTarget.source_model_config_id == ModelConfig.id,
        )
        .where(
            ModelConfig.profile_id == profile_id,
            ModelProxyTarget.target_model_config_id == target_model_id_subquery,
        )
        .distinct()
    )
    if exclude_id is not None:
        query = query.where(ModelConfig.id != exclude_id)
    return list((await db.execute(query)).scalars().all())


async def validate_proxy_model(
    db: AsyncSession,
    *,
    profile_id: int,
    model_type: str,
    proxy_targets: list[ProxyTargetReference],
    api_family: str,
    exclude_model_id: str | None = None,
) -> list[ModelConfig]:
    if model_type == "proxy":
        if not proxy_targets:
            raise HTTPException(
                status_code=400,
                detail="proxy_targets is required for proxy models",
            )
        target_model_ids = [target.target_model_id for target in proxy_targets]
        if exclude_model_id is not None and exclude_model_id in target_model_ids:
            raise HTTPException(
                status_code=400,
                detail="Proxy model cannot target itself",
            )
        target_result = await db.execute(
            select(ModelConfig)
            .options(selectinload(ModelConfig.vendor))
            .where(
                ModelConfig.profile_id == profile_id,
                ModelConfig.model_id.in_(target_model_ids),
            )
        )
        targets_by_model_id = {
            target.model_id: target for target in target_result.scalars().all()
        }
        ordered_targets: list[ModelConfig] = []
        for proxy_target in proxy_targets:
            target = targets_by_model_id.get(proxy_target.target_model_id)
            if target is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Target model '{proxy_target.target_model_id}' not found",
                )
            if target.model_type != "native":
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Target model '{proxy_target.target_model_id}' is not a native model "
                        "(chained proxies not allowed)"
                    ),
                )
            if target.api_family != api_family:
                raise HTTPException(
                    status_code=400,
                    detail="Proxy targets must use the same api_family as the proxy model",
                )
            ordered_targets.append(target)
        return ordered_targets
    if model_type == "native" and proxy_targets:
        raise HTTPException(
            status_code=400,
            detail="proxy_targets must be empty for native models",
        )
    return []


__all__ = [
    "MODEL_CONFIG_DETAIL_OPTIONS",
    "build_model_list_response",
    "ensure_model_id_available",
    "list_proxy_referrers",
    "load_model_config_detail_or_404",
    "validate_proxy_model",
]
