from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import ensure_utc_datetime, utc_now
from app.models.models import (
    Connection,
    ModelConfig,
    RoutingConnectionRuntimeLease,
    RoutingConnectionRuntimeState,
)

from .policy import BanMode
from .types import (
    FailureKind,
    RecoveryStateEntry,
    RuntimeLeaseAcquireResult,
    RuntimeLeaseKind,
    RuntimeReconcileSummary,
)

_WINDOW_DURATION = timedelta(seconds=1)


def runtime_state_to_recovery_entry(
    current_state: RoutingConnectionRuntimeState,
) -> RecoveryStateEntry:
    return {
        "consecutive_failures": current_state.consecutive_failures,
        "blocked_until_at": current_state.blocked_until_at,
        "max_cooldown_strikes": current_state.max_cooldown_strikes,
        "ban_mode": cast(BanMode, current_state.ban_mode),
        "banned_until_at": ensure_utc_datetime(current_state.banned_until_at),
        "last_cooldown_seconds": float(current_state.last_cooldown_seconds),
        "last_failure_kind": cast(FailureKind | None, current_state.last_failure_kind),
        "probe_eligible_logged": current_state.probe_eligible_logged,
    }


def _connection_has_limiter_config(connection: Connection) -> bool:
    return any(
        limiter_value is not None
        for limiter_value in (
            connection.qps_limit,
            connection.max_in_flight_non_stream,
            connection.max_in_flight_stream,
        )
    )


def _lease_cap_for_kind(
    connection: Connection,
    *,
    lease_kind: RuntimeLeaseKind,
) -> int | None:
    if lease_kind == "stream":
        return connection.max_in_flight_stream
    if lease_kind == "non_stream":
        return connection.max_in_flight_non_stream
    return 1


def _is_window_stale(
    *,
    window_started_at: datetime | None,
    now_at: datetime,
) -> bool:
    normalized_window_started_at = ensure_utc_datetime(window_started_at)
    if normalized_window_started_at is None:
        return True
    return now_at - normalized_window_started_at >= _WINDOW_DURATION


def _compact_state_window_if_needed(
    state_row: RoutingConnectionRuntimeState,
    *,
    now_at: datetime,
) -> bool:
    if not _is_window_stale(
        window_started_at=state_row.window_started_at,
        now_at=now_at,
    ):
        return False
    if state_row.window_started_at is None and state_row.window_request_count == 0:
        return False
    state_row.window_started_at = None
    state_row.window_request_count = 0
    return True


def _state_row_is_empty_after_compaction(
    *,
    state_row: RoutingConnectionRuntimeState,
    now_at: datetime,
) -> bool:
    _ = _compact_state_window_if_needed(state_row, now_at=now_at)
    return (
        state_row.in_flight_non_stream == 0
        and state_row.in_flight_stream == 0
        and state_row.window_request_count == 0
        and state_row.consecutive_failures == 0
        and state_row.last_failure_kind is None
        and float(state_row.last_cooldown_seconds) == 0.0
        and state_row.max_cooldown_strikes == 0
        and state_row.ban_mode == "off"
        and state_row.banned_until_at is None
        and state_row.blocked_until_at is None
        and state_row.probe_eligible_logged is False
        and state_row.circuit_state == "closed"
        and state_row.probe_available_at is None
        and state_row.live_p95_latency_ms is None
        and state_row.last_live_failure_kind is None
        and state_row.last_live_failure_at is None
        and state_row.last_live_success_at is None
        and state_row.last_probe_status is None
        and state_row.last_probe_at is None
        and state_row.endpoint_ping_ewma_ms is None
        and state_row.conversation_delay_ewma_ms is None
    )


async def upsert_and_lock_runtime_state(
    *,
    session: AsyncSession,
    profile_id: int,
    connection_id: int,
    now_at: datetime | None = None,
) -> RoutingConnectionRuntimeState:
    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    _ = await session.execute(
        insert(RoutingConnectionRuntimeState)
        .values(
            profile_id=profile_id,
            connection_id=connection_id,
            window_started_at=None,
            window_request_count=0,
            in_flight_non_stream=0,
            in_flight_stream=0,
            consecutive_failures=0,
            last_failure_kind=None,
            last_cooldown_seconds=0.0,
            max_cooldown_strikes=0,
            ban_mode="off",
            banned_until_at=None,
            blocked_until_at=None,
            probe_eligible_logged=False,
            circuit_state="closed",
            probe_available_at=None,
            live_p95_latency_ms=None,
            last_live_failure_kind=None,
            last_live_failure_at=None,
            last_live_success_at=None,
            last_probe_status=None,
            last_probe_at=None,
            endpoint_ping_ewma_ms=None,
            conversation_delay_ewma_ms=None,
            created_at=normalized_now,
            updated_at=normalized_now,
        )
        .on_conflict_do_nothing(index_elements=["profile_id", "connection_id"])
    )
    result = await session.execute(
        select(RoutingConnectionRuntimeState)
        .where(
            RoutingConnectionRuntimeState.profile_id == profile_id,
            RoutingConnectionRuntimeState.connection_id == connection_id,
        )
        .with_for_update()
    )
    return result.scalar_one()


async def get_runtime_states_for_connections(
    db: AsyncSession,
    *,
    profile_id: int,
    connection_ids: list[int],
) -> dict[int, RoutingConnectionRuntimeState]:
    if not connection_ids:
        return {}

    result = await db.execute(
        select(RoutingConnectionRuntimeState).where(
            RoutingConnectionRuntimeState.profile_id == profile_id,
            RoutingConnectionRuntimeState.connection_id.in_(connection_ids),
        )
    )
    rows = list(result.scalars().all())
    return {row.connection_id: row for row in rows}


async def list_runtime_states_for_model(
    db: AsyncSession,
    *,
    profile_id: int,
    model_config_id: int,
) -> list[RoutingConnectionRuntimeState]:
    result = await db.execute(
        select(RoutingConnectionRuntimeState)
        .join(Connection, Connection.id == RoutingConnectionRuntimeState.connection_id)
        .where(
            RoutingConnectionRuntimeState.profile_id == profile_id,
            Connection.profile_id == profile_id,
            Connection.model_config_id == model_config_id,
        )
        .order_by(Connection.priority.asc(), Connection.id.asc())
    )
    return list(result.scalars().all())


async def _release_expired_leases_for_state(
    *,
    session: AsyncSession,
    state_row: RoutingConnectionRuntimeState,
    now_at: datetime,
) -> int:
    expired_leases = list(
        (
            await session.execute(
                select(RoutingConnectionRuntimeLease)
                .where(
                    RoutingConnectionRuntimeLease.profile_id == state_row.profile_id,
                    RoutingConnectionRuntimeLease.connection_id
                    == state_row.connection_id,
                    RoutingConnectionRuntimeLease.expires_at <= now_at,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not expired_leases:
        return 0

    for lease in expired_leases:
        if lease.lease_kind == "stream":
            state_row.in_flight_stream = max(0, state_row.in_flight_stream - 1)
        elif lease.lease_kind == "non_stream":
            state_row.in_flight_non_stream = max(0, state_row.in_flight_non_stream - 1)
        elif state_row.circuit_state == "half_open":
            state_row.circuit_state = "open"
            state_row.probe_available_at = now_at
        await session.delete(lease)
    return len(expired_leases)


async def acquire_connection_lease(
    *,
    session: AsyncSession,
    profile_id: int,
    connection: Connection,
    lease_kind: RuntimeLeaseKind,
    lease_ttl_seconds: int,
    now_at: datetime | None = None,
) -> RuntimeLeaseAcquireResult:
    if lease_kind == "half_open_probe":
        return await acquire_half_open_probe_lease(
            session=session,
            profile_id=profile_id,
            connection_id=connection.id,
            lease_ttl_seconds=lease_ttl_seconds,
            now_at=now_at,
        )

    if not _connection_has_limiter_config(connection):
        return RuntimeLeaseAcquireResult(admitted=True)

    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    state_row = await upsert_and_lock_runtime_state(
        session=session,
        profile_id=profile_id,
        connection_id=connection.id,
        now_at=normalized_now,
    )

    state_changed = False
    expired_released = await _release_expired_leases_for_state(
        session=session,
        state_row=state_row,
        now_at=normalized_now,
    )
    if expired_released > 0:
        state_changed = True
    if _compact_state_window_if_needed(state_row, now_at=normalized_now):
        state_changed = True

    if connection.qps_limit is not None:
        if state_row.window_started_at is None:
            state_row.window_started_at = normalized_now
            state_row.window_request_count = 0
            state_changed = True
        if state_row.window_request_count >= connection.qps_limit:
            if _state_row_is_empty_after_compaction(
                state_row=state_row,
                now_at=normalized_now,
            ):
                await session.delete(state_row)
            elif state_changed:
                state_row.updated_at = normalized_now
            return RuntimeLeaseAcquireResult(
                admitted=False,
                deny_reason="qps_limit",
            )

    lease_cap = _lease_cap_for_kind(connection, lease_kind=lease_kind)
    current_in_flight = (
        state_row.in_flight_stream
        if lease_kind == "stream"
        else state_row.in_flight_non_stream
    )
    if lease_cap is not None and current_in_flight >= lease_cap:
        if _state_row_is_empty_after_compaction(
            state_row=state_row, now_at=normalized_now
        ):
            await session.delete(state_row)
        elif state_changed:
            state_row.updated_at = normalized_now
        return RuntimeLeaseAcquireResult(
            admitted=False,
            deny_reason="in_flight_limit",
        )

    if connection.qps_limit is not None:
        state_row.window_request_count += 1
        if state_row.window_started_at is None:
            state_row.window_started_at = normalized_now

    lease_token: str | None = None
    if lease_cap is not None:
        lease_token = uuid4().hex
        if lease_kind == "stream":
            state_row.in_flight_stream += 1
        else:
            state_row.in_flight_non_stream += 1
        session.add(
            RoutingConnectionRuntimeLease(
                lease_token=lease_token,
                profile_id=profile_id,
                connection_id=connection.id,
                lease_kind=lease_kind,
                expires_at=normalized_now + timedelta(seconds=lease_ttl_seconds),
                heartbeat_at=normalized_now,
                created_at=normalized_now,
                updated_at=normalized_now,
            )
        )

    state_row.updated_at = normalized_now
    return RuntimeLeaseAcquireResult(admitted=True, lease_token=lease_token)


async def heartbeat_connection_lease(
    *,
    session: AsyncSession,
    profile_id: int,
    lease_token: str | None,
    lease_ttl_seconds: int,
    now_at: datetime | None = None,
) -> bool:
    if lease_token is None:
        return False

    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    lease = (
        await session.execute(
            select(RoutingConnectionRuntimeLease)
            .where(
                RoutingConnectionRuntimeLease.lease_token == lease_token,
                RoutingConnectionRuntimeLease.profile_id == profile_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if lease is None or lease.expires_at <= normalized_now:
        return False

    lease.expires_at = normalized_now + timedelta(seconds=lease_ttl_seconds)
    lease.heartbeat_at = normalized_now
    lease.updated_at = normalized_now
    return True


async def release_connection_lease(
    *,
    session: AsyncSession,
    profile_id: int,
    lease_token: str | None,
    now_at: datetime | None = None,
) -> bool:
    if lease_token is None:
        return False

    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    lease = (
        await session.execute(
            select(RoutingConnectionRuntimeLease)
            .where(
                RoutingConnectionRuntimeLease.lease_token == lease_token,
                RoutingConnectionRuntimeLease.profile_id == profile_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if lease is None:
        return False

    state_row = (
        await session.execute(
            select(RoutingConnectionRuntimeState)
            .where(
                RoutingConnectionRuntimeState.profile_id == profile_id,
                RoutingConnectionRuntimeState.connection_id == lease.connection_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    if state_row is not None:
        if lease.lease_kind == "stream":
            state_row.in_flight_stream = max(0, state_row.in_flight_stream - 1)
        elif lease.lease_kind == "non_stream":
            state_row.in_flight_non_stream = max(0, state_row.in_flight_non_stream - 1)
        elif state_row.circuit_state == "half_open":
            state_row.circuit_state = "open"
            state_row.probe_available_at = normalized_now

        if _state_row_is_empty_after_compaction(
            state_row=state_row, now_at=normalized_now
        ):
            await session.delete(state_row)
        else:
            state_row.updated_at = normalized_now

    await session.delete(lease)
    return True


async def acquire_half_open_probe_lease(
    *,
    session: AsyncSession,
    profile_id: int,
    connection_id: int,
    lease_ttl_seconds: int,
    now_at: datetime | None = None,
) -> RuntimeLeaseAcquireResult:
    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    state_row = await upsert_and_lock_runtime_state(
        session=session,
        profile_id=profile_id,
        connection_id=connection_id,
        now_at=normalized_now,
    )

    expired_released = await _release_expired_leases_for_state(
        session=session,
        state_row=state_row,
        now_at=normalized_now,
    )
    blocked_until_at = ensure_utc_datetime(state_row.blocked_until_at)
    probe_available_at = ensure_utc_datetime(state_row.probe_available_at)
    if probe_available_at is None:
        probe_available_at = blocked_until_at

    if state_row.circuit_state == "closed" or (
        blocked_until_at is not None and blocked_until_at > normalized_now
    ):
        if expired_released > 0:
            state_row.updated_at = normalized_now
        elif _state_row_is_empty_after_compaction(
            state_row=state_row,
            now_at=normalized_now,
        ):
            await session.delete(state_row)
        return RuntimeLeaseAcquireResult(
            admitted=False,
            deny_reason="probe_not_ready",
        )

    active_probe = (
        await session.execute(
            select(RoutingConnectionRuntimeLease)
            .where(
                RoutingConnectionRuntimeLease.profile_id == profile_id,
                RoutingConnectionRuntimeLease.connection_id == connection_id,
                RoutingConnectionRuntimeLease.lease_kind == "half_open_probe",
                RoutingConnectionRuntimeLease.expires_at > normalized_now,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if active_probe is not None:
        return RuntimeLeaseAcquireResult(
            admitted=False,
            deny_reason="probe_in_progress",
        )

    if probe_available_at is not None and probe_available_at > normalized_now:
        return RuntimeLeaseAcquireResult(
            admitted=False,
            deny_reason="probe_not_ready",
        )

    lease_token = uuid4().hex
    state_row.circuit_state = "half_open"
    state_row.probe_available_at = normalized_now + timedelta(seconds=lease_ttl_seconds)
    state_row.updated_at = normalized_now
    session.add(
        RoutingConnectionRuntimeLease(
            lease_token=lease_token,
            profile_id=profile_id,
            connection_id=connection_id,
            lease_kind="half_open_probe",
            expires_at=normalized_now + timedelta(seconds=lease_ttl_seconds),
            heartbeat_at=normalized_now,
            created_at=normalized_now,
            updated_at=normalized_now,
        )
    )
    return RuntimeLeaseAcquireResult(admitted=True, lease_token=lease_token)


async def acquire_monitoring_probe_lease(
    *,
    session: AsyncSession,
    profile_id: int,
    connection_id: int,
    lease_ttl_seconds: int,
    interval_seconds: int | None = None,
    now_at: datetime | None = None,
) -> RuntimeLeaseAcquireResult:
    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    state_row = await upsert_and_lock_runtime_state(
        session=session,
        profile_id=profile_id,
        connection_id=connection_id,
        now_at=normalized_now,
    )

    _ = await _release_expired_leases_for_state(
        session=session,
        state_row=state_row,
        now_at=normalized_now,
    )
    blocked_until_at = ensure_utc_datetime(state_row.blocked_until_at)
    probe_available_at = ensure_utc_datetime(state_row.probe_available_at)
    if probe_available_at is None:
        probe_available_at = blocked_until_at

    active_probe = (
        await session.execute(
            select(RoutingConnectionRuntimeLease)
            .where(
                RoutingConnectionRuntimeLease.profile_id == profile_id,
                RoutingConnectionRuntimeLease.connection_id == connection_id,
                RoutingConnectionRuntimeLease.lease_kind == "half_open_probe",
                RoutingConnectionRuntimeLease.expires_at > normalized_now,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if active_probe is not None:
        return RuntimeLeaseAcquireResult(
            admitted=False,
            deny_reason="probe_in_progress",
        )

    if interval_seconds is not None and state_row.circuit_state == "closed":
        last_probe_at = ensure_utc_datetime(state_row.last_probe_at)
        if (
            last_probe_at is not None
            and last_probe_at.timestamp() + interval_seconds
            > normalized_now.timestamp()
        ):
            return RuntimeLeaseAcquireResult(
                admitted=False,
                deny_reason="probe_not_due",
            )

    if state_row.circuit_state == "open":
        if blocked_until_at is not None and blocked_until_at > normalized_now:
            return RuntimeLeaseAcquireResult(
                admitted=False,
                deny_reason="probe_not_ready",
            )
        if probe_available_at is not None and probe_available_at > normalized_now:
            return RuntimeLeaseAcquireResult(
                admitted=False,
                deny_reason="probe_not_ready",
            )
        state_row.circuit_state = "half_open"
        state_row.probe_available_at = normalized_now + timedelta(
            seconds=lease_ttl_seconds
        )
        state_row.updated_at = normalized_now

    lease_token = uuid4().hex
    session.add(
        RoutingConnectionRuntimeLease(
            lease_token=lease_token,
            profile_id=profile_id,
            connection_id=connection_id,
            lease_kind="half_open_probe",
            expires_at=normalized_now + timedelta(seconds=lease_ttl_seconds),
            heartbeat_at=normalized_now,
            created_at=normalized_now,
            updated_at=normalized_now,
        )
    )
    return RuntimeLeaseAcquireResult(admitted=True, lease_token=lease_token)


async def mark_probe_eligible_logged(
    *,
    session: AsyncSession,
    profile_id: int,
    connection_id: int,
    now_at: datetime | None = None,
) -> RecoveryStateEntry | None:
    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    current_state = (
        await session.execute(
            select(RoutingConnectionRuntimeState)
            .where(
                RoutingConnectionRuntimeState.profile_id == profile_id,
                RoutingConnectionRuntimeState.connection_id == connection_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    blocked_until_at = ensure_utc_datetime(
        current_state.blocked_until_at if current_state is not None else None
    )
    if (
        current_state is None
        or current_state.probe_eligible_logged
        or current_state.circuit_state != "open"
        or blocked_until_at is None
        or blocked_until_at > normalized_now
    ):
        return None

    current_state.probe_eligible_logged = True
    current_state.probe_available_at = normalized_now
    current_state.updated_at = normalized_now
    return runtime_state_to_recovery_entry(current_state)


async def record_connection_failure_state(
    *,
    session: AsyncSession,
    profile_id: int,
    connection_id: int,
    failure_kind: FailureKind,
    cooldown_seconds: float,
    strike_incremented: bool,
    ban_mode: str | None,
    ban_duration_seconds: int,
    now_at: datetime | None = None,
) -> RecoveryStateEntry:
    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    current_state = await upsert_and_lock_runtime_state(
        session=session,
        profile_id=profile_id,
        connection_id=connection_id,
        now_at=normalized_now,
    )
    current_state.consecutive_failures += 1
    current_state.last_failure_kind = failure_kind
    current_state.last_cooldown_seconds = max(cooldown_seconds, 0.0)
    current_state.probe_eligible_logged = False
    if strike_incremented:
        current_state.max_cooldown_strikes += 1

    if ban_mode is not None:
        current_state.ban_mode = ban_mode
        current_state.banned_until_at = (
            normalized_now + timedelta(seconds=ban_duration_seconds)
            if ban_mode == "temporary" and ban_duration_seconds > 0
            else None
        )

    if cooldown_seconds <= 0.0:
        current_state.blocked_until_at = None
        current_state.circuit_state = "closed"
        current_state.probe_available_at = None
    else:
        current_state.blocked_until_at = normalized_now + timedelta(
            seconds=cooldown_seconds
        )
        current_state.circuit_state = "open"
        current_state.probe_available_at = current_state.blocked_until_at

    current_state.updated_at = normalized_now
    return runtime_state_to_recovery_entry(current_state)


async def record_connection_recovery_state(
    *,
    session: AsyncSession,
    profile_id: int,
    connection_id: int,
    now_at: datetime | None = None,
) -> RecoveryStateEntry | None:
    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    current_state = (
        await session.execute(
            select(RoutingConnectionRuntimeState)
            .where(
                RoutingConnectionRuntimeState.profile_id == profile_id,
                RoutingConnectionRuntimeState.connection_id == connection_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if current_state is None:
        return None

    snapshot = runtime_state_to_recovery_entry(current_state)
    current_state.consecutive_failures = 0
    current_state.last_failure_kind = None
    current_state.last_cooldown_seconds = 0.0
    current_state.max_cooldown_strikes = 0
    current_state.ban_mode = "off"
    current_state.banned_until_at = None
    current_state.blocked_until_at = None
    current_state.probe_eligible_logged = False
    current_state.circuit_state = "closed"
    current_state.probe_available_at = None

    half_open_probe_leases = list(
        (
            await session.execute(
                select(RoutingConnectionRuntimeLease)
                .where(
                    RoutingConnectionRuntimeLease.profile_id == profile_id,
                    RoutingConnectionRuntimeLease.connection_id == connection_id,
                    RoutingConnectionRuntimeLease.lease_kind == "half_open_probe",
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for lease in half_open_probe_leases:
        await session.delete(lease)

    if _state_row_is_empty_after_compaction(
        state_row=current_state, now_at=normalized_now
    ):
        await session.delete(current_state)
    else:
        current_state.updated_at = normalized_now
    return snapshot


async def apply_fused_monitoring_update(
    *,
    session: AsyncSession,
    profile_id: int,
    connection_id: int,
    last_probe_status: str | None,
    last_probe_at: datetime | None,
    endpoint_ping_ewma_ms: float | None,
    conversation_delay_ewma_ms: float | None,
    live_p95_latency_ms: int | None,
    last_live_failure_kind: str | None,
    last_live_failure_at: datetime | None,
    last_live_success_at: datetime | None,
    now_at: datetime | None = None,
) -> RoutingConnectionRuntimeState:
    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    current_state = await upsert_and_lock_runtime_state(
        session=session,
        profile_id=profile_id,
        connection_id=connection_id,
        now_at=normalized_now,
    )
    current_state.last_probe_status = last_probe_status
    current_state.last_probe_at = ensure_utc_datetime(last_probe_at)
    current_state.endpoint_ping_ewma_ms = endpoint_ping_ewma_ms
    current_state.conversation_delay_ewma_ms = conversation_delay_ewma_ms
    current_state.live_p95_latency_ms = live_p95_latency_ms
    current_state.last_live_failure_kind = last_live_failure_kind
    current_state.last_live_failure_at = ensure_utc_datetime(last_live_failure_at)
    current_state.last_live_success_at = ensure_utc_datetime(last_live_success_at)
    current_state.updated_at = normalized_now
    return current_state


async def clear_connection_runtime_state(
    *,
    session: AsyncSession,
    profile_id: int,
    connection_id: int,
) -> int:
    deleted_leases = await session.execute(
        delete(RoutingConnectionRuntimeLease).where(
            RoutingConnectionRuntimeLease.profile_id == profile_id,
            RoutingConnectionRuntimeLease.connection_id == connection_id,
        )
    )
    deleted_state = await session.execute(
        delete(RoutingConnectionRuntimeState).where(
            RoutingConnectionRuntimeState.profile_id == profile_id,
            RoutingConnectionRuntimeState.connection_id == connection_id,
        )
    )
    return int(getattr(deleted_leases, "rowcount", 0) or 0) + int(
        getattr(deleted_state, "rowcount", 0) or 0
    )


async def clear_connection_runtime_states(
    *,
    session: AsyncSession,
    profile_id: int,
    connection_ids: list[int],
) -> int:
    if not connection_ids:
        return 0

    deleted_leases = await session.execute(
        delete(RoutingConnectionRuntimeLease).where(
            RoutingConnectionRuntimeLease.profile_id == profile_id,
            RoutingConnectionRuntimeLease.connection_id.in_(connection_ids),
        )
    )
    deleted_state = await session.execute(
        delete(RoutingConnectionRuntimeState).where(
            RoutingConnectionRuntimeState.profile_id == profile_id,
            RoutingConnectionRuntimeState.connection_id.in_(connection_ids),
        )
    )
    return int(getattr(deleted_leases, "rowcount", 0) or 0) + int(
        getattr(deleted_state, "rowcount", 0) or 0
    )


async def clear_model_runtime_state(
    *,
    session: AsyncSession,
    profile_id: int,
    model_config_id: int,
) -> int:
    connection_ids = list(
        (
            await session.execute(
                select(Connection.id).where(
                    Connection.profile_id == profile_id,
                    Connection.model_config_id == model_config_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return await clear_connection_runtime_states(
        session=session,
        profile_id=profile_id,
        connection_ids=connection_ids,
    )


async def clear_strategy_runtime_state(
    *,
    session: AsyncSession,
    profile_id: int,
    strategy_id: int,
) -> int:
    connection_ids = list(
        (
            await session.execute(
                select(Connection.id)
                .join(ModelConfig, ModelConfig.id == Connection.model_config_id)
                .where(
                    Connection.profile_id == profile_id,
                    ModelConfig.profile_id == profile_id,
                    ModelConfig.loadbalance_strategy_id == strategy_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return await clear_connection_runtime_states(
        session=session,
        profile_id=profile_id,
        connection_ids=connection_ids,
    )


async def clear_profile_runtime_state(
    *,
    session: AsyncSession,
    profile_id: int,
) -> int:
    deleted_leases = await session.execute(
        delete(RoutingConnectionRuntimeLease).where(
            RoutingConnectionRuntimeLease.profile_id == profile_id,
        )
    )
    deleted_state = await session.execute(
        delete(RoutingConnectionRuntimeState).where(
            RoutingConnectionRuntimeState.profile_id == profile_id,
        )
    )
    return int(getattr(deleted_leases, "rowcount", 0) or 0) + int(
        getattr(deleted_state, "rowcount", 0) or 0
    )


async def _reconcile_state_row(
    *,
    session: AsyncSession,
    state_row: RoutingConnectionRuntimeState,
    now_at: datetime,
    summary: RuntimeReconcileSummary,
) -> None:
    state_changed = False
    expired_released = await _release_expired_leases_for_state(
        session=session,
        state_row=state_row,
        now_at=now_at,
    )
    if expired_released > 0:
        summary["expired_leases_released"] += expired_released
        state_changed = True

    if _compact_state_window_if_needed(state_row, now_at=now_at):
        state_changed = True

    if _state_row_is_empty_after_compaction(state_row=state_row, now_at=now_at):
        await session.delete(state_row)
        summary["state_rows_deleted"] += 1
        return

    if state_changed:
        state_row.updated_at = now_at
        summary["state_rows_updated"] += 1


async def reconcile_connection_runtime_state(
    *,
    session: AsyncSession,
    profile_id: int,
    connection_id: int,
    now_at: datetime | None = None,
) -> RuntimeReconcileSummary:
    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    summary: RuntimeReconcileSummary = {
        "expired_leases_released": 0,
        "state_rows_deleted": 0,
        "state_rows_updated": 0,
    }
    state_row = (
        await session.execute(
            select(RoutingConnectionRuntimeState)
            .where(
                RoutingConnectionRuntimeState.profile_id == profile_id,
                RoutingConnectionRuntimeState.connection_id == connection_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if state_row is None:
        expired_leases = list(
            (
                await session.execute(
                    select(RoutingConnectionRuntimeLease).where(
                        RoutingConnectionRuntimeLease.profile_id == profile_id,
                        RoutingConnectionRuntimeLease.connection_id == connection_id,
                        RoutingConnectionRuntimeLease.expires_at <= normalized_now,
                    )
                )
            )
            .scalars()
            .all()
        )
        for lease in expired_leases:
            await session.delete(lease)
            summary["expired_leases_released"] += 1
        return summary

    await _reconcile_state_row(
        session=session,
        state_row=state_row,
        now_at=normalized_now,
        summary=summary,
    )
    return summary


async def reconcile_all_connection_runtime_state(
    *,
    session: AsyncSession,
    profile_id: int | None = None,
    now_at: datetime | None = None,
) -> RuntimeReconcileSummary:
    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    summary: RuntimeReconcileSummary = {
        "expired_leases_released": 0,
        "state_rows_deleted": 0,
        "state_rows_updated": 0,
    }

    state_query = select(RoutingConnectionRuntimeState)
    if profile_id is not None:
        state_query = state_query.where(
            RoutingConnectionRuntimeState.profile_id == profile_id
        )
    state_rows = list((await session.execute(state_query)).scalars().all())

    for state_row in state_rows:
        await _reconcile_state_row(
            session=session,
            state_row=state_row,
            now_at=normalized_now,
            summary=summary,
        )

    return summary


__all__ = [
    "acquire_connection_lease",
    "acquire_half_open_probe_lease",
    "acquire_monitoring_probe_lease",
    "apply_fused_monitoring_update",
    "clear_connection_runtime_state",
    "clear_connection_runtime_states",
    "clear_model_runtime_state",
    "clear_profile_runtime_state",
    "clear_strategy_runtime_state",
    "get_runtime_states_for_connections",
    "heartbeat_connection_lease",
    "list_runtime_states_for_model",
    "mark_probe_eligible_logged",
    "reconcile_all_connection_runtime_state",
    "reconcile_connection_runtime_state",
    "record_connection_failure_state",
    "record_connection_recovery_state",
    "release_connection_lease",
    "runtime_state_to_recovery_entry",
    "upsert_and_lock_runtime_state",
]
