import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.models import Connection, ModelConfig

logger = logging.getLogger(__name__)

_recovery_state: dict[tuple[int, int], tuple[float, float]] = {}


async def get_model_config_with_connections(
    db: AsyncSession, profile_id: int, model_id: str
) -> ModelConfig | None:
    result = await db.execute(
        select(ModelConfig)
        .options(
            selectinload(ModelConfig.connections).selectinload(Connection.endpoint_rel),
            selectinload(ModelConfig.connections).selectinload(Connection.pricing_template_rel),
            selectinload(ModelConfig.provider),
        )
        .where(
            ModelConfig.profile_id == profile_id,
            ModelConfig.model_id == model_id,
            ModelConfig.is_enabled.is_(True),
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        return None

    if config.model_type == "proxy" and config.redirect_to:
        target_result = await db.execute(
            select(ModelConfig)
            .options(
                selectinload(ModelConfig.connections).selectinload(Connection.endpoint_rel),
                selectinload(ModelConfig.connections).selectinload(Connection.pricing_template_rel),
                selectinload(ModelConfig.provider),
            )
            .where(
                ModelConfig.profile_id == profile_id,
                ModelConfig.model_id == config.redirect_to,
                ModelConfig.is_enabled.is_(True),
            )
        )
        target = target_result.scalar_one_or_none()
        if not target:
            logger.warning(
                "Proxy target model_id=%r not found or disabled for profile_id=%d proxy model_id=%r",
                config.redirect_to,
                profile_id,
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
        "get_active_connections for model %s: %d/%d active",
        model_config.model_id,
        len(active_connections),
        len(model_config.connections),
    )
    return sorted(active_connections, key=lambda connection: connection.priority)


def build_attempt_plan(
    profile_id: int, model_config: ModelConfig, now_mono: float
 ) -> list[Connection]:
    active = get_active_connections(model_config)
    if not active:
        logger.warning(
            "build_attempt_plan: No active connections for profile_id=%d model %s",
            profile_id,
            model_config.model_id,
        )
        return []

    if model_config.lb_strategy == "single":
        logger.debug(
            "build_attempt_plan: single strategy profile_id=%d using connection %d",
            profile_id,
            active[0].id,
        )
        return [active[0]]

    if not model_config.failover_recovery_enabled:
        logger.debug(
            "build_attempt_plan: failover without recovery profile_id=%d trying %d connections",
            profile_id,
            len(active),
        )
        return active

    healthy: list[Connection] = []
    probe_eligible: list[Connection] = []

    for connection in active:
        state = _recovery_state.get((profile_id, connection.id))
        if state is None:
            healthy.append(connection)
        else:
            blocked_until, _ = state
            if now_mono >= blocked_until:
                probe_eligible.append(connection)

    logger.debug(
        "build_attempt_plan: profile_id=%d failover with recovery healthy=%s probe_eligible=%s",
        profile_id,
        [connection.id for connection in healthy],
        [connection.id for connection in probe_eligible],
    )
    return healthy + probe_eligible


def mark_connection_failed(
    profile_id: int,
    connection_id: int,
    cooldown_seconds: float,
    now_mono: float,
) -> None:
    blocked_until = now_mono + cooldown_seconds
    _recovery_state[(profile_id, connection_id)] = (blocked_until, cooldown_seconds)
    logger.info(
        "Connection profile_id=%d connection_id=%d marked failed, cooldown %.0fs, blocked until mono=%.1f",
        profile_id,
        connection_id,
        cooldown_seconds,
        blocked_until,
    )


def mark_connection_recovered(profile_id: int, connection_id: int) -> None:
    key = (profile_id, connection_id)
    if key in _recovery_state:
        del _recovery_state[key]
        logger.info(
            "Connection profile_id=%d connection_id=%d recovered, removed from recovery state",
            profile_id,
            connection_id,
        )
