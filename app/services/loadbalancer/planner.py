import logging
from datetime import datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.time import ensure_utc_datetime, utc_now
from app.models.models import Connection, ModelConfig, ModelProxyTarget

from .policy import resolve_effective_loadbalance_policy
from .runtime_store import get_runtime_states_for_connections
from .scoring import rank_candidates
from .state import claim_round_robin_cursor_position
from .types import AttemptCandidateScoreInput, AttemptPlan

logger = logging.getLogger("app.services.loadbalancer")


class ProxyTargetsUnroutableError(Exception):
    proxy_model_id: str

    def __init__(self, *, proxy_model_id: str):
        self.proxy_model_id = proxy_model_id
        super().__init__(f"Proxy model '{proxy_model_id}' has no routable targets.")


def _resolve_proxy_target_model_id(proxy_target: object) -> str | None:
    direct_target_model_id = getattr(proxy_target, "target_model_id", None)
    if isinstance(direct_target_model_id, str) and direct_target_model_id:
        return direct_target_model_id
    target_model = getattr(proxy_target, "target_model_config", None)
    target_model_id = getattr(target_model, "model_id", None)
    return target_model_id if isinstance(target_model_id, str) else None


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _as_int(value: object, *, default: int = 0) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _is_connection_banned(current_state: object, *, now_at: datetime) -> bool:
    if getattr(current_state, "ban_mode", "off") == "manual":
        return True
    banned_until_at = ensure_utc_datetime(
        getattr(current_state, "banned_until_at", None)
    )
    return banned_until_at is not None and banned_until_at > now_at


def _is_connection_blocked(current_state: object, *, now_at: datetime) -> bool:
    blocked_until_at = ensure_utc_datetime(
        getattr(current_state, "blocked_until_at", None)
    )
    return blocked_until_at is not None and blocked_until_at > now_at


def _is_probe_eligible(current_state: object, *, now_at: datetime) -> bool:
    blocked_until_at = ensure_utc_datetime(
        getattr(current_state, "blocked_until_at", None)
    )
    if blocked_until_at is None or blocked_until_at > now_at:
        return False
    if getattr(current_state, "probe_eligible_logged", False):
        return False
    return getattr(current_state, "circuit_state", "closed") == "open"


def _build_candidate_score_input(
    connection: Connection,
    current_state: object | None,
) -> AttemptCandidateScoreInput:
    return AttemptCandidateScoreInput(
        connection=connection,
        circuit_state=(
            getattr(current_state, "circuit_state", "closed")
            if current_state is not None
            else "closed"
        ),
        blocked_until_at=ensure_utc_datetime(
            getattr(current_state, "blocked_until_at", None)
        ),
        banned_until_at=ensure_utc_datetime(
            getattr(current_state, "banned_until_at", None)
        ),
        probe_available_at=ensure_utc_datetime(
            getattr(current_state, "probe_available_at", None)
        ),
        in_flight_non_stream=_as_int(
            getattr(current_state, "in_flight_non_stream", 0),
            default=0,
        ),
        in_flight_stream=_as_int(
            getattr(current_state, "in_flight_stream", 0), default=0
        ),
        qps_window_count=_as_int(
            getattr(current_state, "window_request_count", 0),
            default=0,
        ),
        live_p95_latency_ms=_as_float(
            getattr(current_state, "live_p95_latency_ms", None)
        ),
        last_live_failure_kind=getattr(current_state, "last_live_failure_kind", None),
        last_live_failure_at=ensure_utc_datetime(
            getattr(current_state, "last_live_failure_at", None)
        ),
        last_live_success_at=ensure_utc_datetime(
            getattr(current_state, "last_live_success_at", None)
        ),
        last_probe_status=getattr(current_state, "last_probe_status", None),
        last_probe_at=ensure_utc_datetime(
            getattr(current_state, "last_probe_at", None)
        ),
        endpoint_ping_ewma_ms=_as_float(
            getattr(current_state, "endpoint_ping_ewma_ms", None)
        ),
        conversation_delay_ewma_ms=_as_float(
            getattr(current_state, "conversation_delay_ewma_ms", None)
        ),
    )


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
        cast(list[object], getattr(config, "proxy_targets", [])),
        key=lambda proxy_target: _as_int(
            getattr(proxy_target, "position", 0), default=0
        ),
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
            advance_round_robin=False,
        )
        if attempt_plan.candidates:
            return target

    logger.warning(
        "Proxy model_id=%r has no enabled target model with live-ranked candidates for profile_id=%d",
        model_id,
        profile_id,
    )
    raise ProxyTargetsUnroutableError(proxy_model_id=model_id)


def get_active_connections(model_config: ModelConfig) -> list[Connection]:
    all_connections = cast(list[Connection], getattr(model_config, "connections", []))
    active_connections = [
        connection
        for connection in all_connections
        if connection.is_active and connection.endpoint_rel is not None
    ]
    logger.debug(
        "get_active_connections for model %s: %d/%d active",
        model_config.model_id,
        len(active_connections),
        len(all_connections),
    )
    return sorted(
        active_connections,
        key=lambda connection: (connection.priority, connection.id),
    )


async def build_attempt_plan(
    db: AsyncSession,
    profile_id: int,
    model_config: ModelConfig,
    now_at: datetime | None = None,
    *,
    advance_round_robin: bool = True,
    is_streaming: bool = False,
) -> AttemptPlan:
    _ = advance_round_robin
    strategy = cast(object | None, getattr(model_config, "loadbalance_strategy", None))
    if strategy is None:
        raise ValueError(
            f"Native model {model_config.model_id!r} is missing loadbalance_strategy"
        )

    policy = resolve_effective_loadbalance_policy(strategy)
    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    active_connections = get_active_connections(model_config)
    if not active_connections:
        logger.warning(
            "build_attempt_plan: No active connections for profile_id=%d model %s",
            profile_id,
            model_config.model_id,
        )
        return AttemptPlan(
            policy=policy,
            candidates=[],
            blocked_connection_ids=[],
            probe_eligible_connection_ids=[],
        )

    state_by_connection_id = await get_runtime_states_for_connections(
        db,
        profile_id=profile_id,
        connection_ids=[connection.id for connection in active_connections],
    )

    blocked_connection_ids: list[int] = []
    probe_eligible_connection_ids: list[int] = []
    candidate_inputs: list[AttemptCandidateScoreInput] = []
    ordered_legacy_connections: list[Connection] = []

    for connection in active_connections:
        current_state = state_by_connection_id.get(connection.id)
        if current_state is not None and (
            _is_connection_banned(current_state, now_at=normalized_now)
            or _is_connection_blocked(current_state, now_at=normalized_now)
        ):
            blocked_connection_ids.append(connection.id)
            continue

        if current_state is not None and _is_probe_eligible(
            current_state,
            now_at=normalized_now,
        ):
            probe_eligible_connection_ids.append(connection.id)

        ordered_legacy_connections.append(connection)
        candidate_inputs.append(_build_candidate_score_input(connection, current_state))

    if policy.strategy_type == "legacy":
        if policy.legacy_strategy_type == "round-robin" and ordered_legacy_connections:
            cursor_position = 0
            if advance_round_robin:
                cursor_position = await claim_round_robin_cursor_position(
                    profile_id=profile_id,
                    model_config_id=model_config.id,
                    connection_count=len(ordered_legacy_connections),
                    now_at=normalized_now,
                )
            if cursor_position > 0:
                ordered_legacy_connections = (
                    ordered_legacy_connections[cursor_position:]
                    + ordered_legacy_connections[:cursor_position]
                )
        return AttemptPlan(
            policy=policy,
            connections=ordered_legacy_connections,
            blocked_connection_ids=blocked_connection_ids,
            probe_eligible_connection_ids=probe_eligible_connection_ids,
        )

    ranked_candidates = rank_candidates(
        policy=policy,
        candidate_inputs=candidate_inputs,
        now_at=normalized_now,
        is_streaming=is_streaming,
    )
    logger.debug(
        "build_attempt_plan: profile_id=%d model=%s candidate_order=%s blocked=%s probe_eligible=%s",
        profile_id,
        model_config.model_id,
        [candidate.connection.id for candidate in ranked_candidates],
        blocked_connection_ids,
        probe_eligible_connection_ids,
    )
    return AttemptPlan(
        policy=policy,
        candidates=ranked_candidates,
        blocked_connection_ids=blocked_connection_ids,
        probe_eligible_connection_ids=probe_eligible_connection_ids,
    )


__all__ = [
    "MODEL_CONFIG_WITH_CONNECTION_OPTIONS",
    "ProxyTargetsUnroutableError",
    "build_attempt_plan",
    "get_active_connections",
    "get_model_config_with_connections",
]
