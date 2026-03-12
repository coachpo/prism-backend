import asyncio
import logging
import random
from typing import Literal, TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.models.models import Connection, ModelConfig

logger = logging.getLogger(__name__)

FailureKind = Literal["transient_http", "auth_like", "connect_error", "timeout"]


class RecoveryStateEntry(TypedDict):
    consecutive_failures: int
    blocked_until_mono: float | None
    last_cooldown_seconds: float
    last_failure_kind: FailureKind | None
    probe_eligible_logged: bool


_settings = get_settings()
_recovery_state: dict[tuple[int, int], RecoveryStateEntry] = {}


def _record_loadbalance_event(**event_payload: object) -> None:
    from app.services.audit_service import record_loadbalance_event

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug(
            "Skipping loadbalance event persistence because no running event loop"
        )
        return

    loop.create_task(record_loadbalance_event(**event_payload))


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
    return sorted(active_connections, key=lambda connection: (connection.priority, connection.id))


def _failover_sort_key(connection: Connection) -> tuple[bool, int, int]:
    return (connection.health_status == "unhealthy", connection.priority, connection.id)


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

    ordered_active = sorted(active, key=_failover_sort_key)

    if not model_config.failover_recovery_enabled:
        logger.debug(
            "build_attempt_plan: failover without recovery profile_id=%d trying %d connections",
            profile_id,
            len(ordered_active),
        )
        return ordered_active

    attempt_plan: list[Connection] = []
    blocked_connection_ids: list[int] = []

    for connection in ordered_active:
        key = (profile_id, connection.id)
        state = _recovery_state.get(key)
        if state is None:
            attempt_plan.append(connection)
            continue

        blocked_until = state["blocked_until_mono"]
        if blocked_until is not None and now_mono < blocked_until:
            blocked_connection_ids.append(connection.id)
            continue

        if blocked_until is not None and not state["probe_eligible_logged"]:
            state["probe_eligible_logged"] = True
            logger.info(
                "Failover transition event=probe_eligible profile_id=%d connection_id=%d failure_kind=%s cooldown_seconds=%.2f consecutive_failures=%d",
                profile_id,
                connection.id,
                state["last_failure_kind"],
                state["last_cooldown_seconds"],
                state["consecutive_failures"],
            )
            _record_loadbalance_event(
                profile_id=profile_id,
                connection_id=connection.id,
                event_type="probe_eligible",
                failure_kind=state["last_failure_kind"],
                consecutive_failures=state["consecutive_failures"],
                cooldown_seconds=state["last_cooldown_seconds"],
                blocked_until_mono=None,
                model_id=model_config.model_id,
                endpoint_id=connection.endpoint_id,
                provider_id=model_config.provider_id,
                failure_threshold=_settings.failover_failure_threshold,
                backoff_multiplier=_settings.failover_backoff_multiplier,
                max_cooldown_seconds=_settings.failover_max_cooldown_seconds,
            )

        attempt_plan.append(connection)

    logger.debug(
        "build_attempt_plan: profile_id=%d failover with recovery attempt_plan=%s blocked=%s",
        profile_id,
        [connection.id for connection in attempt_plan],
        blocked_connection_ids,
    )
    return attempt_plan


def _compute_base_cooldown(
    *,
    base_cooldown_seconds: float,
    consecutive_failures: int,
    failure_kind: FailureKind,
) -> float:
    if failure_kind == "auth_like":
        return float(_settings.failover_auth_error_cooldown_seconds)

    if consecutive_failures < _settings.failover_failure_threshold:
        return 0.0

    exponent = consecutive_failures - _settings.failover_failure_threshold
    transient_cooldown = max(base_cooldown_seconds, 0.0) * (
        _settings.failover_backoff_multiplier**exponent
    )
    return float(min(transient_cooldown, _settings.failover_max_cooldown_seconds))


def _apply_jitter(cooldown_seconds: float) -> float:
    if cooldown_seconds <= 0.0 or _settings.failover_jitter_ratio <= 0.0:
        return cooldown_seconds

    jitter_multiplier = random.uniform(
        max(0.0, 1.0 - _settings.failover_jitter_ratio),
        1.0 + _settings.failover_jitter_ratio,
    )
    return cooldown_seconds * jitter_multiplier


def mark_connection_failed(
    profile_id: int,
    connection_id: int,
    base_cooldown_seconds: float,
    now_mono: float,
    failure_kind: FailureKind,
    model_id: str | None = None,
    endpoint_id: int | None = None,
    provider_id: int | None = None,
) -> None:
    key = (profile_id, connection_id)
    previous_state = _recovery_state.get(key)
    previous_blocked_until = (
        previous_state["blocked_until_mono"] if previous_state is not None else None
    )
    consecutive_failures = (
        previous_state["consecutive_failures"] if previous_state is not None else 0
    ) + 1

    base_cooldown = _compute_base_cooldown(
        base_cooldown_seconds=base_cooldown_seconds,
        consecutive_failures=consecutive_failures,
        failure_kind=failure_kind,
    )
    cooldown_seconds = _apply_jitter(base_cooldown)

    if cooldown_seconds <= 0.0:
        _recovery_state[key] = {
            "consecutive_failures": consecutive_failures,
            "blocked_until_mono": None,
            "last_cooldown_seconds": 0.0,
            "last_failure_kind": failure_kind,
            "probe_eligible_logged": False,
        }
        logger.debug(
            "Failover transition event=not_opened profile_id=%d connection_id=%d failure_kind=%s cooldown_seconds=0.00 consecutive_failures=%d",
            profile_id,
            connection_id,
            failure_kind,
            consecutive_failures,
        )
        _record_loadbalance_event(
            profile_id=profile_id,
            connection_id=connection_id,
            event_type="not_opened",
            failure_kind=failure_kind,
            consecutive_failures=consecutive_failures,
            cooldown_seconds=0.0,
            blocked_until_mono=None,
            model_id=model_id,
            endpoint_id=endpoint_id,
            provider_id=provider_id,
            failure_threshold=_settings.failover_failure_threshold,
            backoff_multiplier=_settings.failover_backoff_multiplier,
            max_cooldown_seconds=_settings.failover_max_cooldown_seconds,
        )
        return

    blocked_until = now_mono + cooldown_seconds
    transition = "opened"
    if previous_blocked_until is not None and now_mono < previous_blocked_until:
        transition = "extended"

    _recovery_state[key] = {
        "consecutive_failures": consecutive_failures,
        "blocked_until_mono": blocked_until,
        "last_cooldown_seconds": cooldown_seconds,
        "last_failure_kind": failure_kind,
        "probe_eligible_logged": False,
    }
    logger.info(
        "Failover transition event=%s profile_id=%d connection_id=%d failure_kind=%s cooldown_seconds=%.2f consecutive_failures=%d blocked_until_mono=%.2f",
        transition,
        profile_id,
        connection_id,
        failure_kind,
        cooldown_seconds,
        consecutive_failures,
        blocked_until,
    )
    _record_loadbalance_event(
        profile_id=profile_id,
        connection_id=connection_id,
        event_type=transition,
        failure_kind=failure_kind,
        consecutive_failures=consecutive_failures,
        cooldown_seconds=cooldown_seconds,
        blocked_until_mono=blocked_until,
        model_id=model_id,
        endpoint_id=endpoint_id,
        provider_id=provider_id,
        failure_threshold=_settings.failover_failure_threshold,
        backoff_multiplier=_settings.failover_backoff_multiplier,
        max_cooldown_seconds=_settings.failover_max_cooldown_seconds,
    )


def mark_connection_recovered(
    profile_id: int,
    connection_id: int,
    model_id: str | None = None,
    endpoint_id: int | None = None,
    provider_id: int | None = None,
) -> None:
    key = (profile_id, connection_id)
    state = _recovery_state.pop(key, None)
    if state is None:
        return

    logger.info(
        "Failover transition event=recovered profile_id=%d connection_id=%d failure_kind=%s cooldown_seconds=%.2f consecutive_failures=%d",
        profile_id,
        connection_id,
        state["last_failure_kind"],
        state["last_cooldown_seconds"],
        state["consecutive_failures"],
    )
    _record_loadbalance_event(
        profile_id=profile_id,
        connection_id=connection_id,
        event_type="recovered",
        failure_kind=state["last_failure_kind"],
        consecutive_failures=state["consecutive_failures"],
        cooldown_seconds=state["last_cooldown_seconds"],
        blocked_until_mono=None,
        model_id=model_id,
        endpoint_id=endpoint_id,
        provider_id=provider_id,
        failure_threshold=_settings.failover_failure_threshold,
        backoff_multiplier=_settings.failover_backoff_multiplier,
        max_cooldown_seconds=_settings.failover_max_cooldown_seconds,
    )
