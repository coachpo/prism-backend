import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.time import ensure_utc_datetime, utc_now
from app.models.models import Connection, ModelConfig, ModelProxyTarget

from .state import get_current_states_for_connections
from .types import AttemptPlan

logger = logging.getLogger("app.services.loadbalancer")


def _is_connection_banned(current_state, *, now_at: datetime) -> bool:
    if getattr(current_state, "ban_mode", "off") == "manual":
        return True
    banned_until_at = ensure_utc_datetime(
        getattr(current_state, "banned_until_at", None)
    )
    return banned_until_at is not None and banned_until_at > now_at


def _resolve_proxy_target_model_id(proxy_target) -> str | None:
    direct_target_model_id = getattr(proxy_target, "target_model_id", None)
    if isinstance(direct_target_model_id, str) and direct_target_model_id:
        return direct_target_model_id
    target_model = getattr(proxy_target, "target_model_config", None)
    target_model_id = getattr(target_model, "model_id", None)
    return target_model_id if isinstance(target_model_id, str) else None


MODEL_CONFIG_WITH_CONNECTION_OPTIONS = (
    selectinload(ModelConfig.proxy_targets).selectinload(
        ModelProxyTarget.target_model_config
    ),
    selectinload(ModelConfig.connections).selectinload(Connection.endpoint_rel),
    selectinload(ModelConfig.connections).selectinload(Connection.pricing_template_rel),
    selectinload(ModelConfig.loadbalance_strategy),
    selectinload(ModelConfig.vendor),
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

    if config.model_type != "proxy":
        return config

    for proxy_target in sorted(
        getattr(config, "proxy_targets", []),
        key=lambda proxy_target: proxy_target.position,
    ):
        target_model_id = _resolve_proxy_target_model_id(proxy_target)
        if target_model_id is None:
            continue
        target = await _load_enabled_model_config(
            db,
            profile_id=profile_id,
            model_id=target_model_id,
        )
        if target is None:
            logger.warning(
                "Proxy target model_id=%r not found or disabled for profile_id=%d proxy model_id=%r",
                target_model_id,
                profile_id,
                model_id,
            )
            continue
        attempt_plan = await build_attempt_plan(
            db,
            profile_id,
            target,
            utc_now(),
        )
        if attempt_plan.connections:
            return target

    logger.warning(
        "Proxy model_id=%r has no enabled target model with an attempt plan for profile_id=%d",
        model_id,
        profile_id,
    )
    return None


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


async def _filter_blocked_connections_preserving_order(
    *,
    db: AsyncSession,
    profile_id: int,
    ordered_connections: list[Connection],
    now_at: datetime | None,
) -> AttemptPlan:
    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    state_by_connection_id = await get_current_states_for_connections(
        db,
        profile_id=profile_id,
        connection_ids=[connection.id for connection in ordered_connections],
    )

    attempt_plan: list[Connection] = []
    blocked_connection_ids: list[int] = []
    probe_eligible_connection_ids: list[int] = []

    for connection in ordered_connections:
        current_state = state_by_connection_id.get(connection.id)
        if current_state is None:
            attempt_plan.append(connection)
            continue

        if _is_connection_banned(current_state, now_at=normalized_now):
            blocked_connection_ids.append(connection.id)
            continue

        blocked_until_at = ensure_utc_datetime(current_state.blocked_until_at)
        if blocked_until_at is not None and normalized_now < blocked_until_at:
            blocked_connection_ids.append(connection.id)
            continue

        if blocked_until_at is not None and not getattr(
            current_state, "probe_eligible_logged", False
        ):
            probe_eligible_connection_ids.append(connection.id)

        attempt_plan.append(connection)

    return AttemptPlan(
        connections=attempt_plan,
        blocked_connection_ids=blocked_connection_ids,
        probe_eligible_connection_ids=probe_eligible_connection_ids,
    )


async def build_attempt_plan(
    db: AsyncSession,
    profile_id: int,
    model_config: ModelConfig,
    now_at: datetime | None = None,
) -> AttemptPlan:
    strategy = model_config.loadbalance_strategy
    if strategy is None:
        raise ValueError(
            f"Native model {model_config.model_id!r} is missing loadbalance_strategy"
        )

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

    if strategy.strategy_type == "single":
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

    if strategy.strategy_type == "fill-first":
        ordered_active = list(active)

        if not strategy.failover_recovery_enabled:
            logger.debug(
                "build_attempt_plan: fill-first without recovery profile_id=%d trying %d connections",
                profile_id,
                len(ordered_active),
            )
            return AttemptPlan(
                connections=ordered_active,
                blocked_connection_ids=[],
                probe_eligible_connection_ids=[],
            )

        plan = await _filter_blocked_connections_preserving_order(
            db=db,
            profile_id=profile_id,
            ordered_connections=ordered_active,
            now_at=now_at,
        )
        logger.debug(
            "build_attempt_plan: profile_id=%d fill-first with recovery attempt_plan=%s blocked=%s",
            profile_id,
            [connection.id for connection in plan.connections],
            plan.blocked_connection_ids,
        )
        return plan

    ordered_active = sorted(active, key=_failover_sort_key)

    if not strategy.failover_recovery_enabled:
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

    plan = await _filter_blocked_connections_preserving_order(
        db=db,
        profile_id=profile_id,
        ordered_connections=ordered_active,
        now_at=now_at,
    )

    logger.debug(
        "build_attempt_plan: profile_id=%d failover with recovery attempt_plan=%s blocked=%s",
        profile_id,
        [connection.id for connection in plan.connections],
        plan.blocked_connection_ids,
    )
    return plan


__all__ = [
    "MODEL_CONFIG_WITH_CONNECTION_OPTIONS",
    "build_attempt_plan",
    "get_active_connections",
    "get_model_config_with_connections",
]
