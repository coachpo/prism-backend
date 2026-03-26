from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.models import Connection, ModelConfig, ModelProxyTarget
from app.schemas.schemas import ModelConfigListResponse

from .query_helpers import build_model_list_response, load_model_config_detail_or_404


async def list_models_for_profile(
    db: AsyncSession,
    *,
    profile_id: int,
    get_model_health_stats_fn,
) -> list[ModelConfigListResponse]:
    result = await db.execute(
        select(ModelConfig)
        .options(
            selectinload(ModelConfig.vendor),
            selectinload(ModelConfig.loadbalance_strategy),
            selectinload(ModelConfig.proxy_targets).selectinload(
                ModelProxyTarget.target_model_config
            ),
            selectinload(ModelConfig.connections),
        )
        .where(ModelConfig.profile_id == profile_id)
        .order_by(ModelConfig.id)
    )
    configs = result.scalars().all()
    health_stats = await get_model_health_stats_fn(db, profile_id=profile_id)
    return [
        build_model_list_response(
            config,
            stats=health_stats.get(config.model_id, {}),
        )
        for config in configs
    ]


async def get_model_detail(
    db: AsyncSession,
    *,
    model_config_id: int,
    profile_id: int,
) -> ModelConfig:
    config = await load_model_config_detail_or_404(
        db,
        model_config_id=model_config_id,
        profile_id=profile_id,
    )
    config.connections.sort(key=lambda connection: (connection.priority, connection.id))
    return config


async def get_models_by_endpoint_for_profile(
    db: AsyncSession,
    *,
    endpoint_id: int,
    profile_id: int,
    get_model_health_stats_fn,
) -> list[ModelConfigListResponse]:
    result = await db.execute(
        select(Connection)
        .options(
            selectinload(Connection.model_config_rel).selectinload(ModelConfig.vendor),
            selectinload(Connection.model_config_rel).selectinload(
                ModelConfig.loadbalance_strategy
            ),
            selectinload(Connection.model_config_rel)
            .selectinload(ModelConfig.proxy_targets)
            .selectinload(ModelProxyTarget.target_model_config),
            selectinload(Connection.pricing_template_rel),
        )
        .where(
            Connection.endpoint_id == endpoint_id,
            Connection.profile_id == profile_id,
        )
    )
    connections = result.scalars().all()
    model_configs = {
        connection.model_config_rel.id: connection.model_config_rel
        for connection in connections
    }
    health_stats = await get_model_health_stats_fn(db, profile_id=profile_id)

    response = []
    for config in model_configs.values():
        stats = health_stats.get(config.model_id, {})
        model_connections = [
            connection
            for connection in connections
            if connection.model_config_id == config.id
        ]
        response.append(
            build_model_list_response(
                config,
                stats=stats,
                connection_count=len(model_connections),
                active_connection_count=sum(
                    1 for connection in model_connections if connection.is_active
                ),
            )
        )

    return sorted(response, key=lambda model: model.model_id)


async def get_models_by_endpoints_for_profile(
    db: AsyncSession,
    *,
    endpoint_ids: list[int],
    profile_id: int,
    get_model_health_stats_fn,
) -> dict[int, list[ModelConfigListResponse]]:
    if not endpoint_ids:
        return {}

    result = await db.execute(
        select(Connection)
        .options(
            selectinload(Connection.model_config_rel).selectinload(ModelConfig.vendor),
            selectinload(Connection.model_config_rel).selectinload(
                ModelConfig.loadbalance_strategy
            ),
            selectinload(Connection.model_config_rel)
            .selectinload(ModelConfig.proxy_targets)
            .selectinload(ModelProxyTarget.target_model_config),
            selectinload(Connection.pricing_template_rel),
        )
        .where(
            Connection.endpoint_id.in_(endpoint_ids),
            Connection.profile_id == profile_id,
        )
    )
    connections = result.scalars().all()
    health_stats = await get_model_health_stats_fn(db, profile_id=profile_id)

    models_by_endpoint: dict[int, list[ModelConfigListResponse]] = {}
    for endpoint_id in endpoint_ids:
        endpoint_connections = [
            connection
            for connection in connections
            if connection.endpoint_id == endpoint_id
        ]
        model_configs = {
            connection.model_config_rel.id: connection.model_config_rel
            for connection in endpoint_connections
        }

        response: list[ModelConfigListResponse] = []
        for config in model_configs.values():
            model_connections = [
                connection
                for connection in endpoint_connections
                if connection.model_config_id == config.id
            ]
            response.append(
                build_model_list_response(
                    config,
                    stats=health_stats.get(config.model_id, {}),
                    connection_count=len(model_connections),
                    active_connection_count=sum(
                        1 for connection in model_connections if connection.is_active
                    ),
                )
            )

        models_by_endpoint[endpoint_id] = sorted(
            response, key=lambda model: model.model_id
        )

    return models_by_endpoint


__all__ = [
    "get_model_detail",
    "get_models_by_endpoint_for_profile",
    "get_models_by_endpoints_for_profile",
    "list_models_for_profile",
]
