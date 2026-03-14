import random

from app.services.loadbalancer_support.events import (
    record_failed_transition,
    record_recovered_transition,
)
from app.services.loadbalancer_support.state import (
    FailureKind,
    RecoveryStateEntry,
    _recovery_state,
    settings,
)


def _compute_base_cooldown(
    *,
    base_cooldown_seconds: float,
    consecutive_failures: int,
    failure_kind: FailureKind,
) -> float:
    if failure_kind == "auth_like":
        return float(settings.failover_auth_error_cooldown_seconds)

    if consecutive_failures < settings.failover_failure_threshold:
        return 0.0

    exponent = consecutive_failures - settings.failover_failure_threshold
    transient_cooldown = max(base_cooldown_seconds, 0.0) * (
        settings.failover_backoff_multiplier**exponent
    )
    return float(min(transient_cooldown, settings.failover_max_cooldown_seconds))


def _apply_jitter(cooldown_seconds: float) -> float:
    if cooldown_seconds <= 0.0 or settings.failover_jitter_ratio <= 0.0:
        return cooldown_seconds

    jitter_multiplier = random.uniform(
        max(0.0, 1.0 - settings.failover_jitter_ratio),
        1.0 + settings.failover_jitter_ratio,
    )
    return cooldown_seconds * jitter_multiplier


def _build_recovery_state_entry(
    *,
    consecutive_failures: int,
    blocked_until_mono: float | None,
    cooldown_seconds: float,
    failure_kind: FailureKind,
) -> RecoveryStateEntry:
    return {
        "consecutive_failures": consecutive_failures,
        "blocked_until_mono": blocked_until_mono,
        "last_cooldown_seconds": cooldown_seconds,
        "last_failure_kind": failure_kind,
        "probe_eligible_logged": False,
    }


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
        _recovery_state[key] = _build_recovery_state_entry(
            consecutive_failures=consecutive_failures,
            blocked_until_mono=None,
            cooldown_seconds=0.0,
            failure_kind=failure_kind,
        )
        record_failed_transition(
            event_type="not_opened",
            profile_id=profile_id,
            connection_id=connection_id,
            failure_kind=failure_kind,
            consecutive_failures=consecutive_failures,
            cooldown_seconds=0.0,
            blocked_until_mono=None,
            model_id=model_id,
            endpoint_id=endpoint_id,
            provider_id=provider_id,
        )
        return

    blocked_until = now_mono + cooldown_seconds
    transition = "opened"
    if previous_blocked_until is not None and now_mono < previous_blocked_until:
        transition = "extended"

    _recovery_state[key] = _build_recovery_state_entry(
        consecutive_failures=consecutive_failures,
        blocked_until_mono=blocked_until,
        cooldown_seconds=cooldown_seconds,
        failure_kind=failure_kind,
    )
    record_failed_transition(
        event_type=transition,
        profile_id=profile_id,
        connection_id=connection_id,
        failure_kind=failure_kind,
        consecutive_failures=consecutive_failures,
        cooldown_seconds=cooldown_seconds,
        blocked_until_mono=blocked_until,
        model_id=model_id,
        endpoint_id=endpoint_id,
        provider_id=provider_id,
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

    record_recovered_transition(
        profile_id=profile_id,
        connection_id=connection_id,
        state=state,
        model_id=model_id,
        endpoint_id=endpoint_id,
        provider_id=provider_id,
    )


__all__ = ["mark_connection_failed", "mark_connection_recovered"]
