from datetime import datetime
from typing import TypedDict

from app.services.background_tasks import background_task_manager
from app.services.loadbalance_event_summary import describe_loadbalance_event
from app.services.loadbalancer_support.state import (
    FailureKind,
    RecoveryStateEntry,
    get_loadbalancer_settings,
    logger,
)


class LoadbalanceEventPayload(TypedDict):
    profile_id: int
    connection_id: int
    event_type: str
    failure_kind: FailureKind | None
    consecutive_failures: int
    cooldown_seconds: float
    blocked_until_at: datetime | None
    model_id: str | None
    endpoint_id: int | None
    provider_id: int | None
    failure_threshold: int
    backoff_multiplier: float
    max_cooldown_seconds: int


def _record_loadbalance_event(event_payload: LoadbalanceEventPayload) -> None:
    from app.services.audit_service import record_loadbalance_event

    event_payload_snapshot: LoadbalanceEventPayload = event_payload.copy()

    async def run_event_persist() -> None:
        await record_loadbalance_event(
            profile_id=event_payload_snapshot["profile_id"],
            connection_id=event_payload_snapshot["connection_id"],
            event_type=event_payload_snapshot["event_type"],
            failure_kind=event_payload_snapshot["failure_kind"],
            consecutive_failures=event_payload_snapshot["consecutive_failures"],
            cooldown_seconds=event_payload_snapshot["cooldown_seconds"],
            blocked_until_mono=(
                event_payload_snapshot["blocked_until_at"].timestamp()
                if event_payload_snapshot["blocked_until_at"] is not None
                else None
            ),
            model_id=event_payload_snapshot["model_id"],
            endpoint_id=event_payload_snapshot["endpoint_id"],
            provider_id=event_payload_snapshot["provider_id"],
            failure_threshold=event_payload_snapshot["failure_threshold"],
            backoff_multiplier=event_payload_snapshot["backoff_multiplier"],
            max_cooldown_seconds=event_payload_snapshot["max_cooldown_seconds"],
        )

    try:
        background_task_manager.enqueue(
            name=(
                "loadbalance-event:"
                f"{event_payload_snapshot['profile_id']}:"
                f"{event_payload_snapshot['connection_id']}:"
                f"{event_payload_snapshot['event_type']}"
            ),
            run=run_event_persist,
            max_retries=0,
        )
    except Exception:
        logger.exception(
            "Failed to enqueue loadbalance event: profile_id=%d connection_id=%d event_type=%s",
            event_payload_snapshot["profile_id"],
            event_payload_snapshot["connection_id"],
            event_payload_snapshot["event_type"],
        )


def _build_event_payload(
    *,
    profile_id: int,
    connection_id: int,
    event_type: str,
    failure_kind: FailureKind | None,
    consecutive_failures: int,
    cooldown_seconds: float,
    blocked_until_at: datetime | None,
    model_id: str | None,
    endpoint_id: int | None,
    provider_id: int | None,
) -> LoadbalanceEventPayload:
    settings = get_loadbalancer_settings()

    return {
        "profile_id": profile_id,
        "connection_id": connection_id,
        "event_type": event_type,
        "failure_kind": failure_kind,
        "consecutive_failures": consecutive_failures,
        "cooldown_seconds": cooldown_seconds,
        "blocked_until_at": blocked_until_at,
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
        _build_event_payload(
            profile_id=profile_id,
            connection_id=connection_id,
            event_type="probe_eligible",
            failure_kind=state["last_failure_kind"],
            consecutive_failures=state["consecutive_failures"],
            cooldown_seconds=state["last_cooldown_seconds"],
            blocked_until_at=None,
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
    blocked_until_at: datetime | None,
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
            "Failover transition event=%s profile_id=%d connection_id=%d failure_kind=%s cooldown_seconds=%.2f consecutive_failures=%d blocked_until_at=%s",
            event_type,
            profile_id,
            connection_id,
            failure_kind,
            cooldown_seconds,
            consecutive_failures,
            blocked_until_at.isoformat() if blocked_until_at is not None else None,
        )

    _record_loadbalance_event(
        _build_event_payload(
            profile_id=profile_id,
            connection_id=connection_id,
            event_type=event_type,
            failure_kind=failure_kind,
            consecutive_failures=consecutive_failures,
            cooldown_seconds=cooldown_seconds,
            blocked_until_at=blocked_until_at,
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
        _build_event_payload(
            profile_id=profile_id,
            connection_id=connection_id,
            event_type="recovered",
            failure_kind=state["last_failure_kind"],
            consecutive_failures=state["consecutive_failures"],
            cooldown_seconds=state["last_cooldown_seconds"],
            blocked_until_at=None,
            model_id=model_id,
            endpoint_id=endpoint_id,
            provider_id=provider_id,
        )
    )


__all__ = [
    "describe_loadbalance_event",
    "record_failed_transition",
    "record_probe_eligible_transition",
    "record_recovered_transition",
]
