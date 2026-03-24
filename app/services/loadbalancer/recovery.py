import random
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.database import AsyncSessionLocal
from app.core.time import ensure_utc_datetime, utc_now
from app.models.models import LoadbalanceCurrentState

from .events import (
    record_failed_transition,
    record_probe_eligible_transition,
    record_recovered_transition,
)
from .state import (
    current_state_to_recovery_entry,
    get_loadbalancer_settings,
)
from .types import FailureKind, RecoveryStateEntry


def _compute_base_cooldown(
    *,
    base_cooldown_seconds: float,
    consecutive_failures: int,
    failure_kind: FailureKind,
) -> float:
    settings = get_loadbalancer_settings()

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
    settings = get_loadbalancer_settings()

    if cooldown_seconds <= 0.0 or settings.failover_jitter_ratio <= 0.0:
        return cooldown_seconds

    jitter_multiplier = random.uniform(
        max(0.0, 1.0 - settings.failover_jitter_ratio),
        1.0 + settings.failover_jitter_ratio,
    )
    return cooldown_seconds * jitter_multiplier


async def mark_probe_eligible_logged(
    *,
    profile_id: int,
    connection_id: int,
    now_at: datetime | None = None,
) -> RecoveryStateEntry | None:
    normalized_now = ensure_utc_datetime(now_at) or utc_now()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LoadbalanceCurrentState)
            .where(
                LoadbalanceCurrentState.profile_id == profile_id,
                LoadbalanceCurrentState.connection_id == connection_id,
            )
            .with_for_update()
        )
        current_state = result.scalar_one_or_none()
        blocked_until_at = ensure_utc_datetime(
            current_state.blocked_until_at if current_state is not None else None
        )
        if (
            current_state is None
            or current_state.probe_eligible_logged
            or blocked_until_at is None
            or blocked_until_at > normalized_now
        ):
            await session.rollback()
            return None

        current_state.probe_eligible_logged = True
        await session.commit()
        return current_state_to_recovery_entry(current_state)


async def claim_probe_eligible(
    *,
    profile_id: int,
    connection_id: int,
    model_id: str,
    endpoint_id: int | None,
    provider_id: int,
    now_at: datetime | None = None,
) -> None:
    claimed_state = await mark_probe_eligible_logged(
        profile_id=profile_id,
        connection_id=connection_id,
        now_at=now_at,
    )
    if claimed_state is None:
        return

    record_probe_eligible_transition(
        profile_id=profile_id,
        connection_id=connection_id,
        state=claimed_state,
        model_id=model_id,
        endpoint_id=endpoint_id,
        provider_id=provider_id,
    )


async def record_connection_failure(
    profile_id: int,
    connection_id: int,
    base_cooldown_seconds: float,
    failure_kind: FailureKind,
    model_id: str | None = None,
    endpoint_id: int | None = None,
    provider_id: int | None = None,
    now_at: datetime | None = None,
) -> None:
    normalized_now = ensure_utc_datetime(now_at) or utc_now()

    async with AsyncSessionLocal() as session:
        _ = await session.execute(
            insert(LoadbalanceCurrentState)
            .values(
                profile_id=profile_id,
                connection_id=connection_id,
                consecutive_failures=0,
                last_failure_kind=None,
                last_cooldown_seconds=0.0,
                blocked_until_at=None,
                probe_eligible_logged=False,
                created_at=normalized_now,
                updated_at=normalized_now,
            )
            .on_conflict_do_nothing(index_elements=["profile_id", "connection_id"])
        )
        result = await session.execute(
            select(LoadbalanceCurrentState)
            .where(
                LoadbalanceCurrentState.profile_id == profile_id,
                LoadbalanceCurrentState.connection_id == connection_id,
            )
            .with_for_update()
        )
        current_state = result.scalar_one()
        previous_blocked_until = current_state.blocked_until_at
        consecutive_failures = current_state.consecutive_failures + 1

        base_cooldown = _compute_base_cooldown(
            base_cooldown_seconds=base_cooldown_seconds,
            consecutive_failures=consecutive_failures,
            failure_kind=failure_kind,
        )
        cooldown_seconds = _apply_jitter(base_cooldown)

        current_state.consecutive_failures = consecutive_failures
        current_state.last_failure_kind = failure_kind
        current_state.last_cooldown_seconds = max(cooldown_seconds, 0.0)
        current_state.probe_eligible_logged = False

        if cooldown_seconds <= 0.0:
            current_state.blocked_until_at = None
            snapshot = current_state_to_recovery_entry(current_state)
            await session.commit()
            record_failed_transition(
                event_type="not_opened",
                profile_id=profile_id,
                connection_id=connection_id,
                failure_kind=failure_kind,
                consecutive_failures=consecutive_failures,
                cooldown_seconds=0.0,
                blocked_until_at=None,
                model_id=model_id,
                endpoint_id=endpoint_id,
                provider_id=provider_id,
            )
            return

        blocked_until_at = normalized_now + timedelta(seconds=cooldown_seconds)
        transition = "opened"
        if (
            previous_blocked_until is not None
            and normalized_now < previous_blocked_until
        ):
            transition = "extended"

        current_state.blocked_until_at = blocked_until_at
        snapshot = current_state_to_recovery_entry(current_state)
        await session.commit()

    record_failed_transition(
        event_type=transition,
        profile_id=profile_id,
        connection_id=connection_id,
        failure_kind=failure_kind,
        consecutive_failures=snapshot["consecutive_failures"],
        cooldown_seconds=snapshot["last_cooldown_seconds"],
        blocked_until_at=snapshot["blocked_until_at"],
        model_id=model_id,
        endpoint_id=endpoint_id,
        provider_id=provider_id,
    )


async def record_connection_recovery(
    profile_id: int,
    connection_id: int,
    model_id: str | None = None,
    endpoint_id: int | None = None,
    provider_id: int | None = None,
) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LoadbalanceCurrentState)
            .where(
                LoadbalanceCurrentState.profile_id == profile_id,
                LoadbalanceCurrentState.connection_id == connection_id,
            )
            .with_for_update()
        )
        current_state = result.scalar_one_or_none()
        if current_state is None:
            await session.rollback()
            return

        state = current_state_to_recovery_entry(current_state)
        await session.delete(current_state)
        await session.commit()

    record_recovered_transition(
        profile_id=profile_id,
        connection_id=connection_id,
        state=state,
        model_id=model_id,
        endpoint_id=endpoint_id,
        provider_id=provider_id,
    )


__all__ = [
    "_apply_jitter",
    "claim_probe_eligible",
    "_compute_base_cooldown",
    "record_connection_failure",
    "record_connection_recovery",
]
