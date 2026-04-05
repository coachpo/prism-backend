from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.time import ensure_utc_datetime, utc_now

from .runtime_store import (
    apply_live_runtime_observation_update,
    upsert_and_lock_runtime_state,
)

_EWMA_ALPHA = 0.5


def _apply_ewma(previous_value: float | None, sample: float | None) -> float | None:
    if sample is None:
        return previous_value
    if previous_value is None:
        return sample
    return ((1.0 - _EWMA_ALPHA) * previous_value) + (_EWMA_ALPHA * sample)


async def record_passive_request_outcome(
    *,
    profile_id: int,
    connection_id: int,
    status_code: int,
    response_time_ms: int,
    success_flag: bool | None,
    observed_at: datetime | None = None,
    session: AsyncSession | None = None,
) -> None:
    normalized_observed_at = ensure_utc_datetime(observed_at) or utc_now()
    is_success = success_flag if success_flag is not None else 200 <= status_code < 300

    if session is None:
        async with AsyncSessionLocal() as managed_session:
            await record_passive_request_outcome(
                profile_id=profile_id,
                connection_id=connection_id,
                status_code=status_code,
                response_time_ms=response_time_ms,
                success_flag=is_success,
                observed_at=normalized_observed_at,
                session=managed_session,
            )
            await managed_session.commit()
            return

    current_state = await upsert_and_lock_runtime_state(
        session=session,
        profile_id=profile_id,
        connection_id=connection_id,
        now_at=normalized_observed_at,
    )
    live_p95_latency_ms = int(
        round(
            _apply_ewma(
                float(current_state.live_p95_latency_ms)
                if current_state.live_p95_latency_ms is not None
                else None,
                float(response_time_ms),
            )
            or 0.0
        )
    )

    last_live_failure_kind = None
    last_live_failure_at = None
    last_live_success_at = current_state.last_live_success_at
    if is_success:
        last_live_success_at = normalized_observed_at
    else:
        last_live_failure_kind = "transient_http"
        last_live_failure_at = normalized_observed_at

    await apply_live_runtime_observation_update(
        session=session,
        profile_id=profile_id,
        connection_id=connection_id,
        live_p95_latency_ms=live_p95_latency_ms,
        last_live_failure_kind=last_live_failure_kind,
        last_live_failure_at=last_live_failure_at,
        last_live_success_at=last_live_success_at,
        now_at=normalized_observed_at,
    )


__all__ = ["record_passive_request_outcome"]
