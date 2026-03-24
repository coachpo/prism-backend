import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.time import ensure_utc_datetime, utc_now
from app.models.models import Connection, ModelConfig

from .state import get_current_states_for_connections
from .types import AttemptPlan

logger = logging.getLogger("app.services.loadbalancer")

MODEL_CONFIG_WITH_CONNECTION_OPTIONS = (
    selectinload(ModelConfig.connections).selectinload(Connection.endpoint_rel),
    selectinload(ModelConfig.connections).selectinload(Connection.pricing_template_rel),
    selectinload(ModelConfig.provider),
)


def _build_model_config_query(profile_id: int, model_id: str):
    return (
        select(ModelConfig)
        .options(*MODEL_CONFIG_WITH_CONNECTION_OPTIONS)
        .where(
            ModelConfig.profile_id == profile_id,
            ModelConfig.model_id == model_id,
            ModelConfig.is_enabled.is_(True),
        )
    )


async def _load_enabled_model_config(
    db: AsyncSession,
    *,
    profile_id: int,
    model_id: str,
) -> ModelConfig | None:
    result = await db.execute(_build_model_config_query(profile_id, model_id))
    return result.scalar_one_or_none()


async def get_model_config_with_connections(
    db: AsyncSession,
    profile_id: int,
    model_id: str,
) -> ModelConfig | None:
    config = await _load_enabled_model_config(
        db,
        profile_id=profile_id,
        model_id=model_id,
    )
    if config is None:
        return None

    if config.model_type != "proxy" or not config.redirect_to:
        return config

    target = await _load_enabled_model_config(
        db,
        profile_id=profile_id,
        model_id=config.redirect_to,
    )
    if target is None:
        logger.warning(
            "Proxy target model_id=%r not found or disabled for profile_id=%d proxy model_id=%r",
            config.redirect_to,
            profile_id,
            model_id,
        )
        return None
    return target


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
    return sorted(
        active_connections,
        key=lambda connection: (connection.priority, connection.id),
    )


def _failover_sort_key(connection: Connection) -> tuple[bool, int, int]:
    return (connection.health_status == "unhealthy", connection.priority, connection.id)


async def build_attempt_plan(
    db: AsyncSession,
    profile_id: int,
    model_config: ModelConfig,
    now_at: datetime | None = None,
) -> AttemptPlan:
    active = get_active_connections(model_config)
    if not active:
        logger.warning(
            "build_attempt_plan: No active connections for profile_id=%d model %s",
            profile_id,
            model_config.model_id,
        )
        return AttemptPlan(
            connections=[],
            blocked_connection_ids=[],
            probe_eligible_connection_ids=[],
        )

    if model_config.lb_strategy == "single":
        logger.debug(
            "build_attempt_plan: single strategy profile_id=%d using connection %d",
            profile_id,
            active[0].id,
        )
        return AttemptPlan(
            connections=[active[0]],
            blocked_connection_ids=[],
            probe_eligible_connection_ids=[],
        )

    ordered_active = sorted(active, key=_failover_sort_key)

    if not model_config.failover_recovery_enabled:
        logger.debug(
            "build_attempt_plan: failover without recovery profile_id=%d trying %d connections",
            profile_id,
            len(ordered_active),
        )
        return AttemptPlan(
            connections=ordered_active,
            blocked_connection_ids=[],
            probe_eligible_connection_ids=[],
        )

    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    state_by_connection_id = await get_current_states_for_connections(
        db,
        profile_id=profile_id,
        connection_ids=[connection.id for connection in ordered_active],
    )

    attempt_plan: list[Connection] = []
    blocked_connection_ids: list[int] = []
    probe_eligible_connection_ids: list[int] = []

    for connection in ordered_active:
        current_state = state_by_connection_id.get(connection.id)
        if current_state is None:
            attempt_plan.append(connection)
            continue

        blocked_until_at = ensure_utc_datetime(current_state.blocked_until_at)
        if blocked_until_at is not None and normalized_now < blocked_until_at:
            blocked_connection_ids.append(connection.id)
            continue

        if blocked_until_at is not None and not current_state.probe_eligible_logged:
            probe_eligible_connection_ids.append(connection.id)

        attempt_plan.append(connection)

    logger.debug(
        "build_attempt_plan: profile_id=%d failover with recovery attempt_plan=%s blocked=%s",
        profile_id,
        [connection.id for connection in attempt_plan],
        blocked_connection_ids,
    )
    return AttemptPlan(
        connections=attempt_plan,
        blocked_connection_ids=blocked_connection_ids,
        probe_eligible_connection_ids=probe_eligible_connection_ids,
    )


__all__ = [
    "MODEL_CONFIG_WITH_CONNECTION_OPTIONS",
    "build_attempt_plan",
    "get_active_connections",
    "get_model_config_with_connections",
]
