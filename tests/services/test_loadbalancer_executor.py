from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from app.models.models import Connection, ModelConfig
from app.services.loadbalancer.policy import resolve_effective_loadbalance_policy
from app.services.loadbalancer.types import (
    AttemptCandidate,
    AttemptCandidateScoreInput,
    AttemptPlan,
)
from tests.loadbalance_strategy_helpers import make_routing_policy_adaptive


def _make_connection(connection_id: int, *, priority: int = 0) -> Connection:
    return cast(
        Connection,
        cast(
            object,
            SimpleNamespace(
                id=connection_id,
                priority=priority,
                is_active=True,
                endpoint_rel=object(),
                endpoint_id=connection_id + 10_000,
                qps_limit=None,
                max_in_flight_non_stream=None,
                max_in_flight_stream=None,
                name=f"connection-{connection_id}",
            ),
        ),
    )


def _make_policy(**overrides):
    return resolve_effective_loadbalance_policy(
        SimpleNamespace(routing_policy=make_routing_policy_adaptive(**overrides))
    )


def _make_model_config(
    *,
    connections: list[Connection],
    routing_policy: dict[str, object],
) -> ModelConfig:
    return cast(
        ModelConfig,
        cast(
            object,
            SimpleNamespace(
                id=501,
                model_id="executor-model",
                connections=connections,
                loadbalance_strategy=SimpleNamespace(routing_policy=routing_policy),
            ),
        ),
    )


def _make_attempt_candidate(connection: Connection) -> AttemptCandidate:
    return AttemptCandidate(
        connection=connection,
        score_input=AttemptCandidateScoreInput(
            connection=connection,
            circuit_state="closed",
            blocked_until_at=None,
            banned_until_at=None,
            probe_available_at=None,
            in_flight_non_stream=0,
            in_flight_stream=0,
            qps_window_count=0,
            live_p95_latency_ms=None,
            last_live_failure_kind=None,
            last_live_failure_at=None,
            last_live_success_at=None,
            last_probe_status=None,
            last_probe_at=None,
            endpoint_ping_ewma_ms=None,
            conversation_delay_ewma_ms=None,
        ),
        score=0.0,
        sort_key=(0.0, getattr(connection, "priority", 0), connection.id),
    )


def _make_attempt_plan(policy, *connections: Connection) -> AttemptPlan:
    return AttemptPlan(
        policy=policy,
        candidates=[_make_attempt_candidate(connection) for connection in connections],
        blocked_connection_ids=[],
        probe_eligible_connection_ids=[],
    )


class TestLoadbalancerExecutor:
    @pytest.mark.asyncio
    async def test_execute_deadline_aware_attempts_exhausts_request_deadline_before_retrying(
        self,
    ):
        from app.services.loadbalancer.executor import (
            AttemptExecutionResult,
            ExecutionCandidate,
            execute_deadline_aware_attempts,
        )

        primary = _make_connection(11, priority=0)
        secondary = _make_connection(12, priority=1)
        policy_document = make_routing_policy_adaptive(
            deadline_budget_ms=20,
            hedge_enabled=False,
        )
        policy = _make_policy(deadline_budget_ms=20, hedge_enabled=False)
        launches: list[int] = []
        cancelled = asyncio.Event()

        async def run_attempt(candidate: ExecutionCandidate, attempt_number: int):
            launches.append(candidate.connection.id)
            assert attempt_number == 1
            try:
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return AttemptExecutionResult(
                attempted=True,
                accepted=False,
                error_detail="primary timed out too late",
            )

        result = await execute_deadline_aware_attempts(
            db=AsyncMock(),
            profile_id=7,
            model_config=_make_model_config(
                connections=[primary, secondary],
                routing_policy=policy_document,
            ),
            policy=policy,
            initial_candidates=[
                ExecutionCandidate(connection=primary, probe_eligible=False)
            ],
            is_streaming=False,
            request_deadline_at_monotonic=time.monotonic() + 0.02,
            run_attempt_fn=run_attempt,
        )

        assert result.response is None
        assert result.deadline_exhausted is True
        assert result.attempt_count == 1
        assert launches == [11]
        assert cancelled.is_set() is True

    @pytest.mark.asyncio
    async def test_execute_deadline_aware_attempts_launches_one_hedge_and_commits_first_winner(
        self,
    ):
        from app.services.loadbalancer.executor import (
            AttemptExecutionResult,
            ExecutionCandidate,
            PreparedExecutionResponse,
            execute_deadline_aware_attempts,
        )

        primary = _make_connection(21, priority=0)
        hedge = _make_connection(22, priority=1)
        extra = _make_connection(23, priority=2)
        policy_document = make_routing_policy_adaptive(
            deadline_budget_ms=500,
            hedge_enabled=True,
            hedge_delay_ms=10,
            max_additional_attempts=1,
        )
        policy = _make_policy(
            deadline_budget_ms=500,
            hedge_enabled=True,
            hedge_delay_ms=10,
            max_additional_attempts=1,
        )
        launches: list[int] = []
        committed: list[tuple[str, int]] = []
        primary_cancelled = asyncio.Event()

        async def primary_attempt(candidate: ExecutionCandidate):
            try:
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                primary_cancelled.set()
                raise
            return AttemptExecutionResult(
                attempted=True,
                accepted=False,
                error_detail="primary should have been cancelled",
            )

        async def run_attempt(candidate: ExecutionCandidate, attempt_number: int):
            launches.append(candidate.connection.id)
            if candidate.connection.id == primary.id:
                return await primary_attempt(candidate)

            async def commit_response(attempt_count: int):
                committed.append(("hedge", attempt_count))
                return {"winner": "hedge"}

            async def discard_response() -> None:
                raise AssertionError("winner must not be discarded")

            await asyncio.sleep(0.01)
            return AttemptExecutionResult(
                attempted=True,
                accepted=True,
                prepared_response=PreparedExecutionResponse(
                    commit_response_fn=commit_response,
                    discard_response_fn=discard_response,
                ),
            )

        with patch(
            "app.services.loadbalancer.executor.build_attempt_plan",
            AsyncMock(return_value=_make_attempt_plan(policy, hedge, extra)),
        ) as build_attempt_plan:
            result = await execute_deadline_aware_attempts(
                db=AsyncMock(),
                profile_id=9,
                model_config=_make_model_config(
                    connections=[primary, hedge, extra],
                    routing_policy=policy_document,
                ),
                policy=policy,
                initial_candidates=[
                    ExecutionCandidate(connection=primary, probe_eligible=False)
                ],
                is_streaming=False,
                request_deadline_at_monotonic=time.monotonic() + 1,
                run_attempt_fn=run_attempt,
            )

        assert result.response == {"winner": "hedge"}
        assert result.deadline_exhausted is False
        assert launches == [21, 22]
        assert committed == [("hedge", 2)]
        assert primary_cancelled.is_set() is True
        build_attempt_plan.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_execute_deadline_aware_attempts_discards_completed_loser_when_two_accept_together(
        self,
    ):
        from app.services.loadbalancer.executor import (
            AttemptExecutionResult,
            ExecutionCandidate,
            PreparedExecutionResponse,
            execute_deadline_aware_attempts,
        )

        primary = _make_connection(31, priority=0)
        hedge = _make_connection(32, priority=1)
        policy_document = make_routing_policy_adaptive(
            deadline_budget_ms=500,
            hedge_enabled=True,
            hedge_delay_ms=0,
            max_additional_attempts=1,
        )
        policy = _make_policy(
            deadline_budget_ms=500,
            hedge_enabled=True,
            hedge_delay_ms=0,
            max_additional_attempts=1,
        )
        ready = asyncio.Event()
        launches: list[int] = []
        commits: list[tuple[str, int]] = []
        discards: list[str] = []

        async def run_attempt(candidate: ExecutionCandidate, attempt_number: int):
            launches.append(candidate.connection.id)
            if len(launches) == 2:
                ready.set()
            await ready.wait()

            connection_name = (
                "primary" if candidate.connection.id == primary.id else "hedge"
            )

            async def commit_response(attempt_count: int):
                commits.append((connection_name, attempt_count))
                return {"winner": connection_name}

            async def discard_response() -> None:
                discards.append(connection_name)

            return AttemptExecutionResult(
                attempted=True,
                accepted=True,
                prepared_response=PreparedExecutionResponse(
                    commit_response_fn=commit_response,
                    discard_response_fn=discard_response,
                ),
            )

        with patch(
            "app.services.loadbalancer.executor.build_attempt_plan",
            AsyncMock(return_value=_make_attempt_plan(policy, hedge)),
        ):
            result = await execute_deadline_aware_attempts(
                db=AsyncMock(),
                profile_id=11,
                model_config=_make_model_config(
                    connections=[primary, hedge],
                    routing_policy=policy_document,
                ),
                policy=policy,
                initial_candidates=[
                    ExecutionCandidate(connection=primary, probe_eligible=False)
                ],
                is_streaming=False,
                request_deadline_at_monotonic=time.monotonic() + 1,
                run_attempt_fn=run_attempt,
            )

        assert result.response == {"winner": "primary"}
        assert launches == [31, 32]
        assert commits == [("primary", 2)]
        assert discards == ["hedge"]

    @pytest.mark.asyncio
    async def test_execute_deadline_aware_attempts_re_ranks_live_candidates_after_failure(
        self,
    ):
        from app.services.loadbalancer.executor import (
            AttemptExecutionResult,
            ExecutionCandidate,
            PreparedExecutionResponse,
            execute_deadline_aware_attempts,
        )

        primary = _make_connection(41, priority=0)
        stale_second = _make_connection(42, priority=1)
        reranked_third = _make_connection(43, priority=2)
        policy_document = make_routing_policy_adaptive(
            deadline_budget_ms=500,
            hedge_enabled=False,
        )
        policy = _make_policy(deadline_budget_ms=500, hedge_enabled=False)
        launches: list[int] = []
        commits: list[tuple[str, int]] = []

        async def run_attempt(candidate: ExecutionCandidate, attempt_number: int):
            launches.append(candidate.connection.id)
            if candidate.connection.id == primary.id:
                return AttemptExecutionResult(
                    attempted=True,
                    accepted=False,
                    error_detail="primary failed and changed live rankings",
                )

            async def commit_response(attempt_count: int):
                commits.append(("reranked-third", attempt_count))
                return {"winner": "reranked-third"}

            async def discard_response() -> None:
                raise AssertionError("reranked winner must not be discarded")

            return AttemptExecutionResult(
                attempted=True,
                accepted=True,
                prepared_response=PreparedExecutionResponse(
                    commit_response_fn=commit_response,
                    discard_response_fn=discard_response,
                ),
            )

        with patch(
            "app.services.loadbalancer.executor.build_attempt_plan",
            AsyncMock(
                return_value=_make_attempt_plan(policy, reranked_third, stale_second)
            ),
        ) as build_attempt_plan:
            result = await execute_deadline_aware_attempts(
                db=AsyncMock(),
                profile_id=13,
                model_config=_make_model_config(
                    connections=[primary, stale_second, reranked_third],
                    routing_policy=policy_document,
                ),
                policy=policy,
                initial_candidates=[
                    ExecutionCandidate(connection=primary, probe_eligible=False),
                    ExecutionCandidate(connection=stale_second, probe_eligible=False),
                    ExecutionCandidate(connection=reranked_third, probe_eligible=False),
                ],
                is_streaming=False,
                request_deadline_at_monotonic=time.monotonic() + 1,
                run_attempt_fn=run_attempt,
            )

        assert result.response == {"winner": "reranked-third"}
        assert launches == [41, 43]
        assert commits == [("reranked-third", 2)]
        build_attempt_plan.assert_awaited_once()
