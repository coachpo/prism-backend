import asyncio

from app.services.loadbalancer_support.state import (
    FailureKind,
    RecoveryStateEntry,
    logger,
    settings,
)


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


def _build_event_payload(
    *,
    profile_id: int,
    connection_id: int,
    event_type: str,
    failure_kind: FailureKind | None,
    consecutive_failures: int,
    cooldown_seconds: float,
    blocked_until_mono: float | None,
    model_id: str | None,
    endpoint_id: int | None,
    provider_id: int | None,
) -> dict[str, object]:
    return {
        "profile_id": profile_id,
        "connection_id": connection_id,
        "event_type": event_type,
        "failure_kind": failure_kind,
        "consecutive_failures": consecutive_failures,
        "cooldown_seconds": cooldown_seconds,
        "blocked_until_mono": blocked_until_mono,
        "model_id": model_id,
        "endpoint_id": endpoint_id,
        "provider_id": provider_id,
        "failure_threshold": settings.failover_failure_threshold,
        "backoff_multiplier": settings.failover_backoff_multiplier,
        "max_cooldown_seconds": settings.failover_max_cooldown_seconds,
    }


def record_probe_eligible_transition(
    *,
    profile_id: int,
    connection_id: int,
    state: RecoveryStateEntry,
    model_id: str,
    endpoint_id: int | None,
    provider_id: int,
) -> None:
    logger.info(
        "Failover transition event=probe_eligible profile_id=%d connection_id=%d failure_kind=%s cooldown_seconds=%.2f consecutive_failures=%d",
        profile_id,
        connection_id,
        state["last_failure_kind"],
        state["last_cooldown_seconds"],
        state["consecutive_failures"],
    )
    _record_loadbalance_event(
        **_build_event_payload(
            profile_id=profile_id,
            connection_id=connection_id,
            event_type="probe_eligible",
            failure_kind=state["last_failure_kind"],
            consecutive_failures=state["consecutive_failures"],
            cooldown_seconds=state["last_cooldown_seconds"],
            blocked_until_mono=None,
            model_id=model_id,
            endpoint_id=endpoint_id,
            provider_id=provider_id,
        )
    )


def record_failed_transition(
    *,
    event_type: str,
    profile_id: int,
    connection_id: int,
    failure_kind: FailureKind,
    consecutive_failures: int,
    cooldown_seconds: float,
    blocked_until_mono: float | None,
    model_id: str | None,
    endpoint_id: int | None,
    provider_id: int | None,
) -> None:
    if event_type == "not_opened":
        logger.debug(
            "Failover transition event=not_opened profile_id=%d connection_id=%d failure_kind=%s cooldown_seconds=0.00 consecutive_failures=%d",
            profile_id,
            connection_id,
            failure_kind,
            consecutive_failures,
        )
    else:
        logger.info(
            "Failover transition event=%s profile_id=%d connection_id=%d failure_kind=%s cooldown_seconds=%.2f consecutive_failures=%d blocked_until_mono=%.2f",
            event_type,
            profile_id,
            connection_id,
            failure_kind,
            cooldown_seconds,
            consecutive_failures,
            blocked_until_mono,
        )

    _record_loadbalance_event(
        **_build_event_payload(
            profile_id=profile_id,
            connection_id=connection_id,
            event_type=event_type,
            failure_kind=failure_kind,
            consecutive_failures=consecutive_failures,
            cooldown_seconds=cooldown_seconds,
            blocked_until_mono=blocked_until_mono,
            model_id=model_id,
            endpoint_id=endpoint_id,
            provider_id=provider_id,
        )
    )


def record_recovered_transition(
    *,
    profile_id: int,
    connection_id: int,
    state: RecoveryStateEntry,
    model_id: str | None,
    endpoint_id: int | None,
    provider_id: int | None,
) -> None:
    logger.info(
        "Failover transition event=recovered profile_id=%d connection_id=%d failure_kind=%s cooldown_seconds=%.2f consecutive_failures=%d",
        profile_id,
        connection_id,
        state["last_failure_kind"],
        state["last_cooldown_seconds"],
        state["consecutive_failures"],
    )
    _record_loadbalance_event(
        **_build_event_payload(
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
        )
    )


__all__ = [
    "record_failed_transition",
    "record_probe_eligible_transition",
    "record_recovered_transition",
]
