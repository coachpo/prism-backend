import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.models import Connection, ModelConfig

logger = logging.getLogger(__name__)

_recovery_state: dict[int, tuple[float, float]] = {}


async def get_model_config_with_connections(
    db: AsyncSession, model_id: str
) -> ModelConfig | None:
    result = await db.execute(
        select(ModelConfig)
        .options(
            selectinload(ModelConfig.connections).selectinload(Connection.endpoint_rel),
            selectinload(ModelConfig.provider),
        )
        .where(ModelConfig.model_id == model_id, ModelConfig.is_enabled.is_(True))
    )
    config = result.scalar_one_or_none()
    if not config:
        return None

    if config.model_type == "proxy" and config.redirect_to:
        target_result = await db.execute(
            select(ModelConfig)
            .options(
                selectinload(ModelConfig.connections).selectinload(
                    Connection.endpoint_rel
                ),
                selectinload(ModelConfig.provider),
            )
            .where(
                ModelConfig.model_id == config.redirect_to,
                ModelConfig.is_enabled.is_(True),
            )
        )
        target = target_result.scalar_one_or_none()
        if not target:
            logger.warning(
                "Proxy target model_id=%r not found or disabled for proxy model_id=%r",
                config.redirect_to,
                model_id,
            )
            return None
        return target

    return config


def get_active_connections(model_config: ModelConfig) -> list[Connection]:
    active_connections = [
        connection
        for connection in model_config.connections
        if connection.is_active and connection.endpoint_rel is not None
    ]
    logger.debug(
        f"get_active_connections for model {model_config.model_id}: "
        f"{len(active_connections)}/{len(model_config.connections)} active"
    )
    return sorted(active_connections, key=lambda connection: connection.priority)


def build_attempt_plan(model_config: ModelConfig, now_mono: float) -> list[Connection]:
    active = get_active_connections(model_config)
    if not active:
        logger.warning(
            f"build_attempt_plan: No active connections for model {model_config.model_id}"
        )
        return []

    if model_config.lb_strategy == "single":
        logger.debug(
            f"build_attempt_plan: single strategy, using connection {active[0].id}"
        )
        return [active[0]]

    if not model_config.failover_recovery_enabled:
        logger.debug(
            f"build_attempt_plan: failover without recovery, trying {len(active)} connections"
        )
        return active

    healthy: list[Connection] = []
    probe_eligible: list[Connection] = []

    for connection in active:
        state = _recovery_state.get(connection.id)
        if state is None:
            healthy.append(connection)
        else:
            blocked_until, _ = state
            if now_mono >= blocked_until:
                probe_eligible.append(connection)

    logger.debug(
        f"build_attempt_plan: failover with recovery, "
        f"healthy={[connection.id for connection in healthy]}, "
        f"probe_eligible={[connection.id for connection in probe_eligible]}"
    )
    return healthy + probe_eligible


def mark_connection_failed(
    connection_id: int, cooldown_seconds: float, now_mono: float
) -> None:
    blocked_until = now_mono + cooldown_seconds
    _recovery_state[connection_id] = (blocked_until, cooldown_seconds)
    logger.info(
        "Connection %d marked failed, cooldown %.0fs, blocked until mono=%.1f",
        connection_id,
        cooldown_seconds,
        blocked_until,
    )


def mark_connection_recovered(connection_id: int) -> None:
    if connection_id in _recovery_state:
        del _recovery_state[connection_id]
        logger.info(
            "Connection %d recovered, removed from recovery state", connection_id
        )


async def get_model_config_with_endpoints(
    db: AsyncSession, model_id: str
) -> ModelConfig | None:
    return await get_model_config_with_connections(db, model_id)


def get_active_endpoints(model_config: ModelConfig) -> list[Connection]:
    return get_active_connections(model_config)


def mark_endpoint_failed(
    endpoint_id: int, cooldown_seconds: float, now_mono: float
) -> None:
    mark_connection_failed(endpoint_id, cooldown_seconds, now_mono)


def mark_endpoint_recovered(endpoint_id: int) -> None:
    mark_connection_recovered(endpoint_id)
