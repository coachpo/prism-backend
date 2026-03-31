from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal
from app.core.time import ensure_utc_datetime, utc_now
from app.models.models import (
    Connection,
    ModelConfig,
    RoutingConnectionRuntimeState,
)
from app.services.loadbalancer.policy import resolve_effective_loadbalance_policy
from app.services.monitoring.probe_runner import run_connection_probe

logger = logging.getLogger(__name__)

DEFAULT_MONITORING_PROBE_INTERVAL_SECONDS = 300
MIN_MONITORING_PROBE_INTERVAL_SECONDS = 30
MAX_MONITORING_PROBE_INTERVAL_SECONDS = 3_600


def _resolve_interval_seconds(value: int | None) -> int:
    if value is None:
        return DEFAULT_MONITORING_PROBE_INTERVAL_SECONDS
    return max(
        MIN_MONITORING_PROBE_INTERVAL_SECONDS,
        min(MAX_MONITORING_PROBE_INTERVAL_SECONDS, int(value)),
    )


@dataclass(frozen=True, slots=True)
class ScheduledProbeCandidate:
    profile_id: int
    connection_id: int
    interval_seconds: int


def _is_connection_due_for_probe(
    state_row: RoutingConnectionRuntimeState | None,
    *,
    interval_seconds: int,
    now_at: datetime,
) -> bool:
    if state_row is None:
        return True

    if state_row.circuit_state == "open":
        probe_available_at = ensure_utc_datetime(state_row.probe_available_at)
        blocked_until_at = ensure_utc_datetime(state_row.blocked_until_at)
        if probe_available_at is None:
            probe_available_at = blocked_until_at
        return probe_available_at is not None and probe_available_at <= now_at

    if state_row.circuit_state == "half_open":
        return False

    last_probe_at = ensure_utc_datetime(state_row.last_probe_at)
    if last_probe_at is None:
        return True
    return last_probe_at + timedelta(seconds=interval_seconds) <= now_at


async def _load_due_probe_candidates(
    session: AsyncSession,
    *,
    now_at: datetime,
) -> list[ScheduledProbeCandidate]:
    connections = list(
        (
            await session.execute(
                select(Connection)
                .options(
                    selectinload(Connection.model_config_rel).selectinload(
                        ModelConfig.loadbalance_strategy
                    ),
                    selectinload(Connection.endpoint_rel),
                )
                .join(ModelConfig, ModelConfig.id == Connection.model_config_id)
                .where(
                    Connection.is_active.is_(True),
                    ModelConfig.is_enabled.is_(True),
                )
                .order_by(
                    Connection.profile_id.asc(),
                    Connection.model_config_id.asc(),
                    Connection.priority.asc(),
                    Connection.id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    if not connections:
        return []

    connection_ids = [connection.id for connection in connections]
    runtime_rows = list(
        (
            await session.execute(
                select(RoutingConnectionRuntimeState).where(
                    RoutingConnectionRuntimeState.connection_id.in_(connection_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    runtime_state_by_connection = {row.connection_id: row for row in runtime_rows}

    candidates: list[ScheduledProbeCandidate] = []
    for connection in connections:
        strategy = getattr(connection.model_config_rel, "loadbalance_strategy", None)
        if strategy is None:
            continue
        policy = resolve_effective_loadbalance_policy(strategy)
        if not policy.monitoring_enabled:
            continue

        interval_seconds = _resolve_interval_seconds(
            getattr(connection, "monitoring_probe_interval_seconds", None)
        )
        state_row = runtime_state_by_connection.get(connection.id)
        if not _is_connection_due_for_probe(
            state_row,
            interval_seconds=interval_seconds,
            now_at=now_at,
        ):
            continue
        candidates.append(
            ScheduledProbeCandidate(
                profile_id=connection.profile_id,
                connection_id=connection.id,
                interval_seconds=interval_seconds,
            )
        )
    return candidates


async def run_monitoring_cycle(
    *,
    http_client: httpx.AsyncClient,
    now_at: datetime | None = None,
    run_connection_probe_fn=run_connection_probe,
) -> float:
    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    async with AsyncSessionLocal() as session:
        candidates = await _load_due_probe_candidates(session, now_at=normalized_now)

    next_sleep_seconds = DEFAULT_MONITORING_PROBE_INTERVAL_SECONDS
    for candidate in candidates:
        next_sleep_seconds = min(next_sleep_seconds, candidate.interval_seconds)
        try:
            async with AsyncSessionLocal() as probe_session:
                await run_connection_probe_fn(
                    db=probe_session,
                    client=http_client,
                    profile_id=candidate.profile_id,
                    connection_id=candidate.connection_id,
                    checked_at=normalized_now,
                    acquire_probe_lease=True,
                )
                await probe_session.commit()
        except Exception:
            logger.exception(
                "Monitoring probe failed: profile_id=%d connection_id=%d",
                candidate.profile_id,
                candidate.connection_id,
            )

    if not candidates:
        return float(DEFAULT_MONITORING_PROBE_INTERVAL_SECONDS)
    return float(next_sleep_seconds)


class MonitoringScheduler:
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        run_cycle_fn: Callable[..., Awaitable[float | None]] = run_monitoring_cycle,
        run_connection_probe_fn=run_connection_probe,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._http_client = http_client
        self._run_cycle_fn = run_cycle_fn
        self._run_connection_probe_fn = run_connection_probe_fn
        self._sleep_fn = sleep_fn
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    @property
    def started(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.started:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run_loop(),
            name="monitoring-scheduler",
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        await self._task
        self._task = None

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            sleep_seconds = await self._run_cycle_fn(
                http_client=self._http_client,
                now_at=None,
                run_connection_probe_fn=self._run_connection_probe_fn,
            )
            if self._stop_event.is_set():
                return

            resolved_sleep_seconds = (
                float(sleep_seconds)
                if isinstance(sleep_seconds, (int, float)) and sleep_seconds > 0
                else 1.0
            )
            await self._sleep_fn(resolved_sleep_seconds)


__all__ = [
    "MonitoringScheduler",
    "run_monitoring_cycle",
]
