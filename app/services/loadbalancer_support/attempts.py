from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import ensure_utc_datetime, utc_now
from app.models.models import Connection, ModelConfig
from app.services.loadbalancer_support.events import record_probe_eligible_transition
from app.services.loadbalancer_support.state import (
    get_current_states_for_connections,
    logger,
    mark_probe_eligible_logged,
)


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

    ordered_active = sorted(active, key=_failover_sort_key)

    if not model_config.failover_recovery_enabled:
        logger.debug(
            "build_attempt_plan: failover without recovery profile_id=%d trying %d connections",
            profile_id,
            len(ordered_active),
        )
        return ordered_active

    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    state_by_connection_id = await get_current_states_for_connections(
        db,
        profile_id=profile_id,
        connection_ids=[connection.id for connection in ordered_active],
    )

    attempt_plan: list[Connection] = []
    blocked_connection_ids: list[int] = []

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
            claimed_state = await mark_probe_eligible_logged(
                profile_id=profile_id,
                connection_id=connection.id,
                now_at=normalized_now,
            )
            if claimed_state is not None:
                record_probe_eligible_transition(
                    profile_id=profile_id,
                    connection_id=connection.id,
                    state=claimed_state,
                    model_id=model_config.model_id,
                    endpoint_id=connection.endpoint_id,
                    provider_id=model_config.provider_id,
                )

        attempt_plan.append(connection)

    logger.debug(
        "build_attempt_plan: profile_id=%d failover with recovery attempt_plan=%s blocked=%s",
        profile_id,
        [connection.id for connection in attempt_plan],
        blocked_connection_ids,
    )
    return attempt_plan


__all__ = ["build_attempt_plan", "get_active_connections"]
