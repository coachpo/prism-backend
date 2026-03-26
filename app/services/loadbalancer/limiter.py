from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, TypedDict
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.database import AsyncSessionLocal
from app.core.time import ensure_utc_datetime, utc_now
from app.models.models import Connection, ConnectionLimiterLease, ConnectionLimiterState

LeaseKind = Literal["stream", "non_stream"]
LimiterDenyReason = Literal["qps_limit", "in_flight_limit"]
_WINDOW_DURATION = timedelta(seconds=1)


@dataclass(frozen=True, slots=True)
class LimiterAcquireResult:
    admitted: bool
    lease_token: str | None = None
    deny_reason: LimiterDenyReason | None = None


class LimiterReconcileSummary(TypedDict):
    expired_leases_released: int
    state_rows_deleted: int
    state_rows_updated: int


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
    lease_kind: LeaseKind,
) -> int | None:
    if lease_kind == "stream":
        return connection.max_in_flight_stream
    return connection.max_in_flight_non_stream


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
    state_row: ConnectionLimiterState,
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


async def _load_or_create_state_row(
    *,
    session,
    profile_id: int,
    connection_id: int,
    now_at: datetime,
) -> ConnectionLimiterState:
    await session.execute(
        insert(ConnectionLimiterState)
        .values(
            profile_id=profile_id,
            connection_id=connection_id,
            window_started_at=None,
            window_request_count=0,
            in_flight_non_stream=0,
            in_flight_stream=0,
            created_at=now_at,
            updated_at=now_at,
        )
        .on_conflict_do_nothing(index_elements=["profile_id", "connection_id"])
    )
    result = await session.execute(
        select(ConnectionLimiterState)
        .where(
            ConnectionLimiterState.profile_id == profile_id,
            ConnectionLimiterState.connection_id == connection_id,
        )
        .with_for_update()
    )
    return result.scalar_one()


async def _release_expired_leases_for_state(
    *,
    session,
    state_row: ConnectionLimiterState,
    now_at: datetime,
) -> int:
    expired_leases = list(
        (
            await session.execute(
                select(ConnectionLimiterLease)
                .where(
                    ConnectionLimiterLease.profile_id == state_row.profile_id,
                    ConnectionLimiterLease.connection_id == state_row.connection_id,
                    ConnectionLimiterLease.expires_at <= now_at,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not expired_leases:
        return 0

    released_stream = sum(1 for lease in expired_leases if lease.lease_kind == "stream")
    released_non_stream = len(expired_leases) - released_stream
    state_row.in_flight_stream = max(0, state_row.in_flight_stream - released_stream)
    state_row.in_flight_non_stream = max(
        0,
        state_row.in_flight_non_stream - released_non_stream,
    )
    for lease in expired_leases:
        await session.delete(lease)
    return len(expired_leases)


def _state_row_is_idle_after_compaction(
    *,
    state_row: ConnectionLimiterState,
    now_at: datetime,
) -> bool:
    _compact_state_window_if_needed(state_row, now_at=now_at)
    return (
        state_row.in_flight_non_stream == 0
        and state_row.in_flight_stream == 0
        and state_row.window_request_count == 0
    )


async def _reconcile_state_row(
    *,
    session,
    state_row: ConnectionLimiterState,
    now_at: datetime,
    summary: LimiterReconcileSummary,
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

    if _state_row_is_idle_after_compaction(
        state_row=state_row,
        now_at=now_at,
    ):
        await session.delete(state_row)
        summary["state_rows_deleted"] += 1
        return

    if state_changed:
        state_row.updated_at = now_at
        summary["state_rows_updated"] += 1


async def acquire_connection_limit(
    *,
    profile_id: int,
    connection: Connection,
    lease_kind: LeaseKind,
    lease_ttl_seconds: int,
    now_at: datetime | None = None,
) -> LimiterAcquireResult:
    if not _connection_has_limiter_config(connection):
        return LimiterAcquireResult(admitted=True)

    normalized_now = ensure_utc_datetime(now_at) or utc_now()

    async with AsyncSessionLocal() as session:
        state_row = await _load_or_create_state_row(
            session=session,
            profile_id=profile_id,
            connection_id=connection.id,
            now_at=normalized_now,
        )

        await _release_expired_leases_for_state(
            session=session,
            state_row=state_row,
            now_at=normalized_now,
        )
        _compact_state_window_if_needed(state_row, now_at=normalized_now)

        if connection.qps_limit is not None:
            if state_row.window_started_at is None:
                state_row.window_started_at = normalized_now
                state_row.window_request_count = 0
            if state_row.window_request_count >= connection.qps_limit:
                await session.rollback()
                return LimiterAcquireResult(
                    admitted=False,
                    deny_reason="qps_limit",
                )

        lease_cap = _lease_cap_for_kind(connection, lease_kind=lease_kind)
        if lease_kind == "stream":
            if lease_cap is not None and state_row.in_flight_stream >= lease_cap:
                await session.rollback()
                return LimiterAcquireResult(
                    admitted=False,
                    deny_reason="in_flight_limit",
                )
        else:
            if lease_cap is not None and state_row.in_flight_non_stream >= lease_cap:
                await session.rollback()
                return LimiterAcquireResult(
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
                ConnectionLimiterLease(
                    lease_token=lease_token,
                    profile_id=profile_id,
                    connection_id=connection.id,
                    lease_kind=lease_kind,
                    expires_at=normalized_now + timedelta(seconds=lease_ttl_seconds),
                    created_at=normalized_now,
                    updated_at=normalized_now,
                )
            )

        state_row.updated_at = normalized_now
        await session.commit()
        return LimiterAcquireResult(admitted=True, lease_token=lease_token)


async def release_connection_lease(
    *,
    profile_id: int,
    lease_token: str | None,
    now_at: datetime | None = None,
) -> bool:
    if lease_token is None:
        return False

    normalized_now = ensure_utc_datetime(now_at) or utc_now()

    async with AsyncSessionLocal() as session:
        lease = (
            await session.execute(
                select(ConnectionLimiterLease)
                .where(
                    ConnectionLimiterLease.lease_token == lease_token,
                    ConnectionLimiterLease.profile_id == profile_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if lease is None:
            await session.rollback()
            return False

        state_row = (
            await session.execute(
                select(ConnectionLimiterState)
                .where(
                    ConnectionLimiterState.profile_id == profile_id,
                    ConnectionLimiterState.connection_id == lease.connection_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

        if state_row is not None:
            if lease.lease_kind == "stream":
                state_row.in_flight_stream = max(0, state_row.in_flight_stream - 1)
            else:
                state_row.in_flight_non_stream = max(
                    0,
                    state_row.in_flight_non_stream - 1,
                )
            if _state_row_is_idle_after_compaction(
                state_row=state_row,
                now_at=normalized_now,
            ):
                await session.delete(state_row)
            else:
                state_row.updated_at = normalized_now

        await session.delete(lease)
        await session.commit()
        return True


async def reconcile_connection_limit(
    *,
    profile_id: int,
    connection_id: int,
    now_at: datetime | None = None,
) -> LimiterReconcileSummary:
    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    summary: LimiterReconcileSummary = {
        "expired_leases_released": 0,
        "state_rows_deleted": 0,
        "state_rows_updated": 0,
    }

    async with AsyncSessionLocal() as session:
        state_row = (
            await session.execute(
                select(ConnectionLimiterState)
                .where(
                    ConnectionLimiterState.profile_id == profile_id,
                    ConnectionLimiterState.connection_id == connection_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if state_row is None:
            expired_leases = list(
                (
                    await session.execute(
                        select(ConnectionLimiterLease).where(
                            ConnectionLimiterLease.profile_id == profile_id,
                            ConnectionLimiterLease.connection_id == connection_id,
                            ConnectionLimiterLease.expires_at <= normalized_now,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for lease in expired_leases:
                await session.delete(lease)
                summary["expired_leases_released"] += 1
            await session.commit()
            return summary

        await _reconcile_state_row(
            session=session,
            state_row=state_row,
            now_at=normalized_now,
            summary=summary,
        )
        await session.commit()

    return summary


async def reconcile_all_connection_limits(
    *,
    profile_id: int | None = None,
    now_at: datetime | None = None,
) -> LimiterReconcileSummary:
    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    summary: LimiterReconcileSummary = {
        "expired_leases_released": 0,
        "state_rows_deleted": 0,
        "state_rows_updated": 0,
    }

    async with AsyncSessionLocal() as session:
        state_query = select(ConnectionLimiterState)
        if profile_id is not None:
            state_query = state_query.where(
                ConnectionLimiterState.profile_id == profile_id
            )
        state_rows = list((await session.execute(state_query)).scalars().all())

        for state_row in state_rows:
            await _reconcile_state_row(
                session=session,
                state_row=state_row,
                now_at=normalized_now,
                summary=summary,
            )

        await session.commit()

    return summary


__all__ = [
    "LeaseKind",
    "LimiterAcquireResult",
    "LimiterDenyReason",
    "LimiterReconcileSummary",
    "acquire_connection_limit",
    "reconcile_all_connection_limits",
    "reconcile_connection_limit",
    "release_connection_lease",
]
