from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal
from app.core.time import ensure_utc_datetime, utc_now
from app.models.models import (
    Connection,
    ModelConfig,
    MonitoringConnectionProbeResult,
)
from app.services.loadbalancer.policy import (
    EffectiveLoadbalancePolicy,
    resolve_effective_loadbalance_policy,
)
from app.services.loadbalancer.recovery import (
    _apply_jitter,
    _compute_base_cooldown,
    _should_increment_max_cooldown_strike,
)
from app.services.loadbalancer.runtime_store import (
    apply_fused_monitoring_update,
    record_connection_failure_state,
    record_connection_recovery_state,
    upsert_and_lock_runtime_state,
)
from app.services.loadbalancer.types import FailureKind

_EWMA_ALPHA = 0.5


def _apply_ewma(previous_value: float | None, sample: float | None) -> float | None:
    if sample is None:
        return previous_value
    if previous_value is None:
        return sample
    return ((1.0 - _EWMA_ALPHA) * previous_value) + (_EWMA_ALPHA * sample)


def _resolve_fused_probe_status(
    endpoint_ping_status: str,
    conversation_status: str,
) -> str:
    if endpoint_ping_status == "healthy" and conversation_status == "healthy":
        return "healthy"
    if endpoint_ping_status == "healthy" or conversation_status == "healthy":
        return "degraded"
    return "unhealthy"


def _normalize_failure_kind(value: str | None) -> FailureKind:
    if value == "timeout":
        return "timeout"
    if value == "connect_error":
        return "connect_error"
    return "transient_http"


async def _load_connection_policy(
    session: AsyncSession,
    *,
    profile_id: int,
    connection_id: int,
) -> EffectiveLoadbalancePolicy:
    connection = (
        await session.execute(
            select(Connection)
            .options(
                selectinload(Connection.model_config_rel).selectinload(
                    ModelConfig.loadbalance_strategy
                )
            )
            .where(
                Connection.profile_id == profile_id,
                Connection.id == connection_id,
            )
        )
    ).scalar_one()
    strategy = getattr(connection.model_config_rel, "loadbalance_strategy", None)
    if strategy is None:
        raise ValueError(
            f"Connection {connection_id} is missing a load balance strategy policy"
        )
    return resolve_effective_loadbalance_policy(strategy)


async def _apply_probe_feedback(
    session: AsyncSession,
    *,
    profile_id: int,
    connection_id: int,
    policy: EffectiveLoadbalancePolicy,
    endpoint_ping_status: str,
    endpoint_ping_ms: int | None,
    conversation_status: str,
    conversation_delay_ms: int | None,
    failure_kind: str | None,
    checked_at: datetime,
) -> str:
    current_state = await upsert_and_lock_runtime_state(
        session=session,
        profile_id=profile_id,
        connection_id=connection_id,
        now_at=checked_at,
    )
    fused_status = _resolve_fused_probe_status(
        endpoint_ping_status,
        conversation_status,
    )

    if fused_status == "healthy":
        if current_state.circuit_state != "closed":
            _ = await record_connection_recovery_state(
                session=session,
                profile_id=profile_id,
                connection_id=connection_id,
                now_at=checked_at,
            )
        current_state = await upsert_and_lock_runtime_state(
            session=session,
            profile_id=profile_id,
            connection_id=connection_id,
            now_at=checked_at,
        )
    else:
        normalized_failure_kind = _normalize_failure_kind(failure_kind)
        next_consecutive_failures = max(
            current_state.consecutive_failures + 1,
            policy.failover_failure_threshold,
        )
        cooldown_seconds = _apply_jitter(
            _compute_base_cooldown(
                policy=policy,
                base_cooldown_seconds=policy.failover_cooldown_seconds,
                consecutive_failures=next_consecutive_failures,
                failure_kind=normalized_failure_kind,
            ),
            policy=policy,
        )
        strike_incremented = _should_increment_max_cooldown_strike(
            base_cooldown_seconds=policy.failover_cooldown_seconds,
            consecutive_failures=next_consecutive_failures,
            failure_kind=normalized_failure_kind,
            previous_consecutive_failures=current_state.consecutive_failures,
            previous_failure_kind=cast(
                FailureKind | None,
                current_state.last_failure_kind,
            ),
            policy=policy,
        )
        projected_strikes = current_state.max_cooldown_strikes + (
            1 if strike_incremented else 0
        )
        ban_mode: str | None = None
        ban_duration_seconds = 0
        if (
            policy.failover_ban_mode != "off"
            and strike_incremented
            and projected_strikes >= policy.failover_max_cooldown_strikes_before_ban
        ):
            ban_mode = policy.failover_ban_mode
            ban_duration_seconds = policy.failover_ban_duration_seconds

        _ = await record_connection_failure_state(
            session=session,
            profile_id=profile_id,
            connection_id=connection_id,
            failure_kind=normalized_failure_kind,
            cooldown_seconds=cooldown_seconds,
            strike_incremented=strike_incremented,
            ban_mode=ban_mode,
            ban_duration_seconds=ban_duration_seconds,
            now_at=checked_at,
        )
        current_state = await upsert_and_lock_runtime_state(
            session=session,
            profile_id=profile_id,
            connection_id=connection_id,
            now_at=checked_at,
        )

    endpoint_ping_ewma_ms = _apply_ewma(
        float(current_state.endpoint_ping_ewma_ms)
        if current_state.endpoint_ping_ewma_ms is not None
        else None,
        float(endpoint_ping_ms) if endpoint_ping_ms is not None else None,
    )
    conversation_delay_ewma_ms = _apply_ewma(
        float(current_state.conversation_delay_ewma_ms)
        if current_state.conversation_delay_ewma_ms is not None
        else None,
        float(conversation_delay_ms) if conversation_delay_ms is not None else None,
    )
    last_live_failure_kind = current_state.last_live_failure_kind
    last_live_failure_at = current_state.last_live_failure_at
    last_live_success_at = current_state.last_live_success_at

    if fused_status == "healthy":
        last_live_success_at = checked_at
    else:
        last_live_failure_kind = _normalize_failure_kind(failure_kind)
        last_live_failure_at = checked_at

    _ = await apply_fused_monitoring_update(
        session=session,
        profile_id=profile_id,
        connection_id=connection_id,
        last_probe_status=fused_status,
        last_probe_at=checked_at,
        endpoint_ping_ewma_ms=endpoint_ping_ewma_ms,
        conversation_delay_ewma_ms=conversation_delay_ewma_ms,
        live_p95_latency_ms=current_state.live_p95_latency_ms,
        last_live_failure_kind=last_live_failure_kind,
        last_live_failure_at=last_live_failure_at,
        last_live_success_at=last_live_success_at,
        now_at=checked_at,
    )
    return fused_status


async def record_probe_outcome(
    *,
    profile_id: int,
    vendor_id: int,
    model_config_id: int,
    connection_id: int,
    endpoint_id: int,
    endpoint_ping_status: str,
    endpoint_ping_ms: int | None,
    conversation_status: str,
    conversation_delay_ms: int | None,
    failure_kind: str | None,
    detail: str | None,
    checked_at: datetime | None = None,
    session: AsyncSession | None = None,
) -> str:
    normalized_checked_at = ensure_utc_datetime(checked_at) or utc_now()

    if session is None:
        async with AsyncSessionLocal() as managed_session:
            result = await record_probe_outcome(
                profile_id=profile_id,
                vendor_id=vendor_id,
                model_config_id=model_config_id,
                connection_id=connection_id,
                endpoint_id=endpoint_id,
                endpoint_ping_status=endpoint_ping_status,
                endpoint_ping_ms=endpoint_ping_ms,
                conversation_status=conversation_status,
                conversation_delay_ms=conversation_delay_ms,
                failure_kind=failure_kind,
                detail=detail,
                checked_at=normalized_checked_at,
                session=managed_session,
            )
            await managed_session.commit()
            return result

    session.add(
        MonitoringConnectionProbeResult(
            profile_id=profile_id,
            vendor_id=vendor_id,
            model_config_id=model_config_id,
            connection_id=connection_id,
            endpoint_id=endpoint_id,
            endpoint_ping_status=endpoint_ping_status,
            endpoint_ping_ms=endpoint_ping_ms,
            conversation_status=conversation_status,
            conversation_delay_ms=conversation_delay_ms,
            failure_kind=failure_kind,
            detail=detail,
            checked_at=normalized_checked_at,
        )
    )
    policy = await _load_connection_policy(
        session,
        profile_id=profile_id,
        connection_id=connection_id,
    )
    return await _apply_probe_feedback(
        session,
        profile_id=profile_id,
        connection_id=connection_id,
        policy=policy,
        endpoint_ping_status=endpoint_ping_status,
        endpoint_ping_ms=endpoint_ping_ms,
        conversation_status=conversation_status,
        conversation_delay_ms=conversation_delay_ms,
        failure_kind=failure_kind,
        checked_at=normalized_checked_at,
    )


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

    _ = await apply_fused_monitoring_update(
        session=session,
        profile_id=profile_id,
        connection_id=connection_id,
        last_probe_status=current_state.last_probe_status,
        last_probe_at=current_state.last_probe_at,
        endpoint_ping_ewma_ms=(
            float(current_state.endpoint_ping_ewma_ms)
            if current_state.endpoint_ping_ewma_ms is not None
            else None
        ),
        conversation_delay_ewma_ms=(
            float(current_state.conversation_delay_ewma_ms)
            if current_state.conversation_delay_ewma_ms is not None
            else None
        ),
        live_p95_latency_ms=live_p95_latency_ms,
        last_live_failure_kind=last_live_failure_kind,
        last_live_failure_at=last_live_failure_at,
        last_live_success_at=last_live_success_at,
        now_at=normalized_observed_at,
    )


__all__ = [
    "record_passive_request_outcome",
    "record_probe_outcome",
]
