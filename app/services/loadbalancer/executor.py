from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Awaitable, Callable, Coroutine, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Connection, ModelConfig

from .planner import build_attempt_plan
from .policy import EffectiveLoadbalancePolicy, serialize_routing_policy


@dataclass(frozen=True, slots=True)
class ExecutionCandidate:
    connection: Connection
    probe_eligible: bool


@dataclass(frozen=True, slots=True)
class PreparedExecutionResponse:
    commit_response_fn: Callable[[int], Awaitable[object]]
    discard_response_fn: Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class AttemptExecutionResult:
    attempted: bool
    accepted: bool
    limiter_denied: bool = False
    prepared_response: PreparedExecutionResponse | None = None
    error_detail: str | None = None


@dataclass(frozen=True, slots=True)
class DeadlineAwareExecutionResult:
    response: object | None
    attempted_any_endpoint: bool
    limiter_denied_any_endpoint: bool
    deadline_exhausted: bool
    last_error: str | None
    attempt_count: int


@dataclass(slots=True)
class _InFlightAttempt:
    attempt_number: int
    candidate: ExecutionCandidate
    task: asyncio.Task[AttemptExecutionResult]


AttemptRunnerFn = Callable[
    [ExecutionCandidate, int],
    Coroutine[object, object, AttemptExecutionResult],
]


def _candidate_from_plan(
    connection: Connection,
    *,
    probe_eligible_connection_ids: set[int],
) -> ExecutionCandidate:
    return ExecutionCandidate(
        connection=connection,
        probe_eligible=connection.id in probe_eligible_connection_ids,
    )


def _snapshot_model_config(
    *,
    connections: list[Connection],
    model_config: ModelConfig,
    policy: EffectiveLoadbalancePolicy,
) -> ModelConfig:
    return cast(
        ModelConfig,
        cast(
            object,
            SimpleNamespace(
                id=getattr(model_config, "id", None),
                model_id=model_config.model_id,
                connections=list(connections),
                loadbalance_strategy=SimpleNamespace(
                    routing_policy=serialize_routing_policy(policy),
                ),
            ),
        ),
    )


def _hedge_budget(policy: EffectiveLoadbalancePolicy) -> int:
    if not policy.hedge_enabled or policy.max_additional_attempts < 1:
        return 0
    return 1


def _can_use_live_planner_rerank(connections: list[Connection]) -> bool:
    return all(
        isinstance(getattr(connection, "id", None), int)
        and isinstance(getattr(connection, "priority", 0), int)
        and not isinstance(getattr(connection, "priority", 0), bool)
        for connection in connections
    )


def _remaining_seconds(
    *,
    request_deadline_at_monotonic: float,
    monotonic_fn: Callable[[], float],
) -> float:
    return max(request_deadline_at_monotonic - monotonic_fn(), 0.0)


async def _discard_result_if_needed(result: AttemptExecutionResult) -> None:
    prepared_response = result.prepared_response
    if not result.accepted or prepared_response is None:
        return
    await prepared_response.discard_response_fn()


async def _cancel_inflight_attempts(
    inflight_attempts: dict[int, _InFlightAttempt],
) -> None:
    if not inflight_attempts:
        return

    launched_attempts = list(inflight_attempts.values())
    for launched in launched_attempts:
        if not launched.task.done():
            launched.task.cancel()

    settled = await asyncio.gather(
        *(launched.task for launched in launched_attempts),
        return_exceptions=True,
    )
    for outcome in settled:
        if isinstance(outcome, AttemptExecutionResult):
            await _discard_result_if_needed(outcome)


async def _pick_live_candidate(
    *,
    connections: list[Connection],
    db: AsyncSession,
    excluded_connection_ids: set[int],
    is_streaming: bool,
    model_config: ModelConfig,
    policy: EffectiveLoadbalancePolicy,
    profile_id: int,
) -> ExecutionCandidate | None:
    if not _can_use_live_planner_rerank(connections):
        for connection in connections:
            if connection.id in excluded_connection_ids:
                continue
            return ExecutionCandidate(connection=connection, probe_eligible=False)
        return None

    plan = await build_attempt_plan(
        db,
        profile_id,
        _snapshot_model_config(
            connections=connections,
            model_config=model_config,
            policy=policy,
        ),
        now_at=None,
        is_streaming=is_streaming,
    )
    probe_eligible_connection_ids = set(plan.probe_eligible_connection_ids)
    for connection in plan.connections:
        if connection.id in excluded_connection_ids:
            continue
        return _candidate_from_plan(
            connection,
            probe_eligible_connection_ids=probe_eligible_connection_ids,
        )
    return None


async def execute_deadline_aware_attempts(
    *,
    db: AsyncSession,
    profile_id: int,
    model_config: ModelConfig,
    policy: EffectiveLoadbalancePolicy,
    initial_candidates: list[ExecutionCandidate],
    is_streaming: bool,
    request_deadline_at_monotonic: float,
    run_attempt_fn: AttemptRunnerFn,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> DeadlineAwareExecutionResult:
    attempt_count = 0
    attempted_any_endpoint = False
    attempted_connection_ids: set[int] = set()
    deadline_exhausted = False
    inflight_attempts: dict[int, _InFlightAttempt] = {}
    last_error: str | None = None
    limiter_denied_any_endpoint = False
    remaining_hedges = _hedge_budget(policy)
    initial_primary = initial_candidates[0] if initial_candidates else None

    deadline_seconds = _remaining_seconds(
        request_deadline_at_monotonic=request_deadline_at_monotonic,
        monotonic_fn=monotonic_fn,
    )
    if deadline_seconds <= 0:
        return DeadlineAwareExecutionResult(
            response=None,
            attempted_any_endpoint=False,
            limiter_denied_any_endpoint=False,
            deadline_exhausted=True,
            last_error="request deadline exhausted before the first attempt",
            attempt_count=0,
        )

    deadline_task = asyncio.create_task(asyncio.sleep(deadline_seconds))
    hedge_timer: asyncio.Task[None] | None = None
    candidate_connections = list(getattr(model_config, "connections", [])) or [
        candidate.connection for candidate in initial_candidates
    ]

    async def launch_candidate(candidate: ExecutionCandidate) -> None:
        nonlocal attempt_count
        attempt_count += 1
        attempted_connection_ids.add(candidate.connection.id)
        inflight_attempts[candidate.connection.id] = _InFlightAttempt(
            attempt_number=attempt_count,
            candidate=candidate,
            task=asyncio.create_task(run_attempt_fn(candidate, attempt_count)),
        )

    async def maybe_launch_next_candidate(*, use_initial_primary: bool) -> bool:
        if use_initial_primary:
            candidate = initial_primary
            if candidate is None or candidate.connection.id in attempted_connection_ids:
                return False
        else:
            candidate = await _pick_live_candidate(
                connections=candidate_connections,
                db=db,
                excluded_connection_ids=attempted_connection_ids
                | set(inflight_attempts.keys()),
                is_streaming=is_streaming,
                model_config=model_config,
                policy=policy,
                profile_id=profile_id,
            )
            if candidate is None:
                return False

        await launch_candidate(candidate)
        return True

    def maybe_arm_hedge_timer() -> None:
        nonlocal hedge_timer
        if (
            hedge_timer is not None
            or remaining_hedges < 1
            or len(inflight_attempts) != 1
        ):
            return
        remaining_seconds = _remaining_seconds(
            request_deadline_at_monotonic=request_deadline_at_monotonic,
            monotonic_fn=monotonic_fn,
        )
        if remaining_seconds <= 0:
            return
        hedge_timer = asyncio.create_task(
            asyncio.sleep(min(policy.hedge_delay_ms / 1000.0, remaining_seconds))
        )

    try:
        launched = await maybe_launch_next_candidate(use_initial_primary=True)
        if not launched:
            return DeadlineAwareExecutionResult(
                response=None,
                attempted_any_endpoint=False,
                limiter_denied_any_endpoint=False,
                deadline_exhausted=False,
                last_error=None,
                attempt_count=0,
            )
        maybe_arm_hedge_timer()

        while inflight_attempts:
            wait_set: set[asyncio.Task[object]] = {deadline_task}
            if hedge_timer is not None:
                wait_set.add(hedge_timer)
            wait_set.update(
                cast(asyncio.Task[object], launched.task)
                for launched in inflight_attempts.values()
            )

            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

            if deadline_task in done:
                deadline_exhausted = True
                last_error = "request deadline exhausted before any candidate won"
                break

            if hedge_timer is not None and hedge_timer in done:
                hedge_timer = None
                if remaining_hedges > 0:
                    candidate = await _pick_live_candidate(
                        connections=candidate_connections,
                        db=db,
                        excluded_connection_ids=attempted_connection_ids
                        | set(inflight_attempts.keys()),
                        is_streaming=is_streaming,
                        model_config=model_config,
                        policy=policy,
                        profile_id=profile_id,
                    )
                    if candidate is not None:
                        remaining_hedges -= 1
                        await launch_candidate(candidate)
                continue

            completed_attempts = sorted(
                (
                    launched
                    for launched in list(inflight_attempts.values())
                    if launched.task in done
                ),
                key=lambda launched: launched.attempt_number,
            )

            winner: AttemptExecutionResult | None = None
            for launched in completed_attempts:
                inflight_attempts.pop(launched.candidate.connection.id, None)
                outcome = await launched.task
                attempted_any_endpoint = attempted_any_endpoint or outcome.attempted
                limiter_denied_any_endpoint = (
                    limiter_denied_any_endpoint or outcome.limiter_denied
                )

                if outcome.accepted:
                    if outcome.prepared_response is None:
                        raise ValueError(
                            "Accepted attempt results must include prepared_response"
                        )
                    if winner is None:
                        winner = outcome
                    else:
                        await _discard_result_if_needed(outcome)
                    continue

                if outcome.error_detail:
                    last_error = outcome.error_detail

            if winner is not None:
                await _cancel_inflight_attempts(inflight_attempts)
                inflight_attempts.clear()
                prepared_response = winner.prepared_response
                if prepared_response is None:
                    raise ValueError(
                        "Accepted attempt results must include prepared_response"
                    )
                response = await prepared_response.commit_response_fn(attempt_count)
                return DeadlineAwareExecutionResult(
                    response=response,
                    attempted_any_endpoint=attempted_any_endpoint,
                    limiter_denied_any_endpoint=limiter_denied_any_endpoint,
                    deadline_exhausted=False,
                    last_error=None,
                    attempt_count=attempt_count,
                )

            if not inflight_attempts:
                launched = await maybe_launch_next_candidate(use_initial_primary=False)
                if not launched:
                    break
                maybe_arm_hedge_timer()
                continue

            maybe_arm_hedge_timer()

        if deadline_exhausted:
            await _cancel_inflight_attempts(inflight_attempts)
            inflight_attempts.clear()

        return DeadlineAwareExecutionResult(
            response=None,
            attempted_any_endpoint=attempted_any_endpoint,
            limiter_denied_any_endpoint=limiter_denied_any_endpoint,
            deadline_exhausted=deadline_exhausted,
            last_error=last_error,
            attempt_count=attempt_count,
        )
    finally:
        if hedge_timer is not None:
            hedge_timer.cancel()
            await asyncio.gather(hedge_timer, return_exceptions=True)
        deadline_task.cancel()
        await asyncio.gather(deadline_task, return_exceptions=True)


__all__ = [
    "AttemptExecutionResult",
    "DeadlineAwareExecutionResult",
    "ExecutionCandidate",
    "execute_deadline_aware_attempts",
    "PreparedExecutionResponse",
]
