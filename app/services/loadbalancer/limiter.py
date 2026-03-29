from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from app.core.database import AsyncSessionLocal
from app.models.models import Connection

from .runtime_store import (
    acquire_connection_lease,
    heartbeat_connection_lease as heartbeat_runtime_lease,
    reconcile_all_connection_runtime_state,
    reconcile_connection_runtime_state,
    release_connection_lease as release_runtime_lease,
)
from .types import RuntimeReconcileSummary

LeaseKind = Literal["stream", "non_stream"]
LimiterDenyReason = Literal["qps_limit", "in_flight_limit"]


@dataclass(frozen=True, slots=True)
class LimiterAcquireResult:
    admitted: bool
    lease_token: str | None = None
    deny_reason: LimiterDenyReason | None = None


LimiterReconcileSummary = RuntimeReconcileSummary


async def acquire_connection_limit(
    *,
    profile_id: int,
    connection: Connection,
    lease_kind: LeaseKind,
    lease_ttl_seconds: int,
    now_at: datetime | None = None,
) -> LimiterAcquireResult:
    async with AsyncSessionLocal() as session:
        result = await acquire_connection_lease(
            session=session,
            profile_id=profile_id,
            connection=connection,
            lease_kind=lease_kind,
            lease_ttl_seconds=lease_ttl_seconds,
            now_at=now_at,
        )
        await session.commit()
        return LimiterAcquireResult(
            admitted=result.admitted,
            lease_token=result.lease_token,
            deny_reason=cast(LimiterDenyReason | None, result.deny_reason),
        )


async def heartbeat_connection_lease(
    *,
    profile_id: int,
    lease_token: str | None,
    lease_ttl_seconds: int,
    now_at: datetime | None = None,
) -> bool:
    async with AsyncSessionLocal() as session:
        heartbeated = await heartbeat_runtime_lease(
            session=session,
            profile_id=profile_id,
            lease_token=lease_token,
            lease_ttl_seconds=lease_ttl_seconds,
            now_at=now_at,
        )
        await session.commit()
        return heartbeated


async def release_connection_lease(
    *,
    profile_id: int,
    lease_token: str | None,
    now_at: datetime | None = None,
) -> bool:
    async with AsyncSessionLocal() as session:
        released = await release_runtime_lease(
            session=session,
            profile_id=profile_id,
            lease_token=lease_token,
            now_at=now_at,
        )
        await session.commit()
        return released


async def reconcile_connection_limit(
    *,
    profile_id: int,
    connection_id: int,
    now_at: datetime | None = None,
) -> LimiterReconcileSummary:
    async with AsyncSessionLocal() as session:
        summary = await reconcile_connection_runtime_state(
            session=session,
            profile_id=profile_id,
            connection_id=connection_id,
            now_at=now_at,
        )
        await session.commit()
        return summary


async def reconcile_all_connection_limits(
    *,
    profile_id: int | None = None,
    now_at: datetime | None = None,
) -> LimiterReconcileSummary:
    async with AsyncSessionLocal() as session:
        summary = await reconcile_all_connection_runtime_state(
            session=session,
            profile_id=profile_id,
            now_at=now_at,
        )
        await session.commit()
        return summary


__all__ = [
    "LeaseKind",
    "LimiterAcquireResult",
    "LimiterDenyReason",
    "LimiterReconcileSummary",
    "acquire_connection_limit",
    "heartbeat_connection_lease",
    "reconcile_all_connection_limits",
    "reconcile_connection_limit",
    "release_connection_lease",
]
