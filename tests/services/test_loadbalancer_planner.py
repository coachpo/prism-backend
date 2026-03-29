from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.models import Connection, ModelConfig
from app.services.loadbalancer.policy import resolve_effective_loadbalance_policy
from app.services.loadbalancer.types import (
    AttemptCandidate,
    AttemptCandidateScoreInput,
    AttemptPlan,
)
from tests.loadbalance_strategy_helpers import make_routing_policy_adaptive


def _make_connection(
    connection_id: int,
    *,
    priority: int,
    health_status: str = "unknown",
    qps_limit: int | None = None,
    max_in_flight_non_stream: int | None = None,
    max_in_flight_stream: int | None = None,
) -> Connection:
    return cast(
        Connection,
        cast(
            object,
            SimpleNamespace(
                id=connection_id,
                priority=priority,
                health_status=health_status,
                is_active=True,
                endpoint_rel=object(),
                endpoint_id=connection_id + 1_000,
                qps_limit=qps_limit,
                max_in_flight_non_stream=max_in_flight_non_stream,
                max_in_flight_stream=max_in_flight_stream,
                name=f"connection-{connection_id}",
            ),
        ),
    )


def _make_runtime_state(
    *,
    circuit_state: str = "closed",
    blocked_until_at: datetime | None = None,
    banned_until_at: datetime | None = None,
    probe_available_at: datetime | None = None,
    probe_eligible_logged: bool = False,
    in_flight_non_stream: int = 0,
    in_flight_stream: int = 0,
    window_request_count: int = 0,
    live_p95_latency_ms: int | None = None,
    last_live_failure_kind: str | None = None,
    last_live_failure_at: datetime | None = None,
    last_live_success_at: datetime | None = None,
    last_probe_status: str | None = None,
    last_probe_at: datetime | None = None,
    endpoint_ping_ewma_ms: float | None = None,
    conversation_delay_ewma_ms: float | None = None,
):
    return SimpleNamespace(
        circuit_state=circuit_state,
        blocked_until_at=blocked_until_at,
        banned_until_at=banned_until_at,
        probe_available_at=probe_available_at,
        probe_eligible_logged=probe_eligible_logged,
        in_flight_non_stream=in_flight_non_stream,
        in_flight_stream=in_flight_stream,
        window_request_count=window_request_count,
        live_p95_latency_ms=live_p95_latency_ms,
        last_live_failure_kind=last_live_failure_kind,
        last_live_failure_at=last_live_failure_at,
        last_live_success_at=last_live_success_at,
        last_probe_status=last_probe_status,
        last_probe_at=last_probe_at,
        endpoint_ping_ewma_ms=endpoint_ping_ewma_ms,
        conversation_delay_ewma_ms=conversation_delay_ewma_ms,
    )


def _make_model_config(
    *,
    connections: list[Connection],
    routing_policy: dict[str, object] | None = None,
) -> ModelConfig:
    vendor = SimpleNamespace(
        id=88,
        key="vendor-rollup-does-not-route",
        name="Vendor Rollup",
        endpoint_ping_ewma_ms=1,
        conversation_delay_ewma_ms=1,
    )
    return cast(
        ModelConfig,
        cast(
            object,
            SimpleNamespace(
                id=501,
                model_id="adaptive-model",
                model_type="native",
                vendor_id=vendor.id,
                vendor=vendor,
                monitoring_overview=SimpleNamespace(
                    endpoint_ping_ewma_ms=9_999,
                    conversation_delay_ewma_ms=9_999,
                ),
                loadbalance_strategy=SimpleNamespace(
                    routing_policy=routing_policy or make_routing_policy_adaptive()
                ),
                connections=connections,
            ),
        ),
    )


def _make_ranked_candidate(connection: Connection) -> AttemptCandidate:
    score_input = AttemptCandidateScoreInput(
        connection=connection,
        circuit_state="closed",
        blocked_until_at=None,
        banned_until_at=None,
        probe_available_at=None,
        in_flight_non_stream=0,
        in_flight_stream=0,
        qps_window_count=0,
        live_p95_latency_ms=100.0,
        last_live_failure_kind=None,
        last_live_failure_at=None,
        last_live_success_at=None,
        last_probe_status=None,
        last_probe_at=None,
        endpoint_ping_ewma_ms=None,
        conversation_delay_ewma_ms=None,
    )
    return AttemptCandidate(
        connection=connection,
        score_input=score_input,
        score=100.0,
        sort_key=(100.0, getattr(connection, "priority", 0), connection.id),
    )


class TestLoadbalancerPlanner:
    def test_get_active_connections_sorts_active_connections_by_priority_then_id(self):
        from app.services.loadbalancer.planner import get_active_connections

        inactive = cast(
            Connection,
            cast(
                object,
                SimpleNamespace(
                    id=4,
                    priority=0,
                    is_active=False,
                    endpoint_rel=object(),
                ),
            ),
        )
        low_id = _make_connection(7, priority=0)
        high_id = _make_connection(9, priority=0)
        later_priority = _make_connection(11, priority=1)
        model_config = _make_model_config(
            connections=[later_priority, inactive, high_id, low_id]
        )

        ordered = get_active_connections(model_config)

        assert [connection.id for connection in ordered] == [7, 9, 11]

    @pytest.mark.asyncio
    async def test_build_attempt_plan_ranks_with_live_runtime_state_not_health_status_or_priority(
        self,
    ):
        from app.services.loadbalancer.planner import build_attempt_plan

        now_at = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        static_primary = _make_connection(11, priority=0, health_status="healthy")
        middle = _make_connection(12, priority=1, health_status="healthy")
        unhealthy_but_best = _make_connection(13, priority=2, health_status="unhealthy")
        model_config = _make_model_config(
            connections=[middle, unhealthy_but_best, static_primary]
        )

        state_by_connection_id = {
            11: _make_runtime_state(
                live_p95_latency_ms=260,
                last_live_failure_kind="transient_http",
                last_live_failure_at=now_at - timedelta(seconds=5),
                endpoint_ping_ewma_ms=240.0,
                conversation_delay_ewma_ms=380.0,
                last_probe_status="healthy",
                last_probe_at=now_at - timedelta(seconds=20),
            ),
            12: _make_runtime_state(
                live_p95_latency_ms=140,
                endpoint_ping_ewma_ms=120.0,
                conversation_delay_ewma_ms=180.0,
                last_probe_status="healthy",
                last_probe_at=now_at - timedelta(seconds=20),
            ),
            13: _make_runtime_state(
                live_p95_latency_ms=70,
                endpoint_ping_ewma_ms=40.0,
                conversation_delay_ewma_ms=85.0,
                last_probe_status="healthy",
                last_probe_at=now_at - timedelta(seconds=20),
            ),
        }

        with patch(
            "app.services.loadbalancer.planner.get_runtime_states_for_connections",
            AsyncMock(return_value=state_by_connection_id),
        ) as get_states:
            plan = await build_attempt_plan(
                db=AsyncMock(),
                profile_id=5,
                model_config=model_config,
                now_at=now_at,
            )

        get_states.assert_awaited_once()
        assert [candidate.connection.id for candidate in plan.candidates] == [
            13,
            12,
            11,
        ]
        assert [connection.id for connection in plan.connections] == [13, 12, 11]
        assert plan.blocked_connection_ids == []
        assert plan.probe_eligible_connection_ids == []

    @pytest.mark.asyncio
    async def test_build_attempt_plan_filters_hard_exclusions_and_marks_probe_eligible_connections(
        self,
    ):
        from app.services.loadbalancer.planner import build_attempt_plan

        now_at = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        blocked = _make_connection(21, priority=0)
        stable = _make_connection(22, priority=1)
        probe_eligible = _make_connection(23, priority=2)
        banned = _make_connection(24, priority=3)
        model_config = _make_model_config(
            connections=[probe_eligible, stable, blocked, banned]
        )

        state_by_connection_id = {
            21: _make_runtime_state(
                circuit_state="open",
                blocked_until_at=now_at + timedelta(minutes=5),
                probe_available_at=now_at + timedelta(minutes=5),
            ),
            22: _make_runtime_state(
                live_p95_latency_ms=120,
                endpoint_ping_ewma_ms=100.0,
                conversation_delay_ewma_ms=150.0,
                last_probe_status="healthy",
                last_probe_at=now_at - timedelta(seconds=5),
            ),
            23: _make_runtime_state(
                circuit_state="open",
                blocked_until_at=now_at - timedelta(seconds=1),
                probe_available_at=now_at - timedelta(seconds=1),
                live_p95_latency_ms=110,
                endpoint_ping_ewma_ms=90.0,
                conversation_delay_ewma_ms=140.0,
                last_probe_status="healthy",
                last_probe_at=now_at - timedelta(seconds=5),
            ),
            24: _make_runtime_state(
                banned_until_at=now_at + timedelta(minutes=10),
            ),
        }

        with patch(
            "app.services.loadbalancer.planner.get_runtime_states_for_connections",
            AsyncMock(return_value=state_by_connection_id),
        ):
            plan = await build_attempt_plan(
                db=AsyncMock(),
                profile_id=5,
                model_config=model_config,
                now_at=now_at,
            )

        assert [connection.id for connection in plan.connections] == [22, 23]
        assert plan.blocked_connection_ids == [21, 24]
        assert plan.probe_eligible_connection_ids == [23]

    @pytest.mark.asyncio
    async def test_build_attempt_plan_keeps_policy_snapshot_immutable_while_ranking_live_per_evaluation(
        self,
    ):
        from app.services.loadbalancer.planner import build_attempt_plan

        now_at = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        first = _make_connection(31, priority=0)
        second = _make_connection(32, priority=1)
        strategy = SimpleNamespace(
            routing_policy=make_routing_policy_adaptive(deadline_budget_ms=30_000)
        )
        model_config = cast(
            ModelConfig,
            cast(
                object,
                SimpleNamespace(
                    id=502,
                    model_id="snapshot-model",
                    model_type="native",
                    vendor_id=1,
                    vendor=SimpleNamespace(id=1, key="vendor", name="Vendor"),
                    loadbalance_strategy=strategy,
                    connections=[first, second],
                ),
            ),
        )

        first_state = {
            31: _make_runtime_state(
                live_p95_latency_ms=80,
                endpoint_ping_ewma_ms=55.0,
                conversation_delay_ewma_ms=90.0,
                last_probe_status="healthy",
                last_probe_at=now_at - timedelta(seconds=5),
            ),
            32: _make_runtime_state(
                live_p95_latency_ms=180,
                endpoint_ping_ewma_ms=160.0,
                conversation_delay_ewma_ms=220.0,
                last_probe_status="healthy",
                last_probe_at=now_at - timedelta(seconds=5),
            ),
        }
        second_state = {
            31: _make_runtime_state(
                live_p95_latency_ms=240,
                last_live_failure_kind="timeout",
                last_live_failure_at=now_at + timedelta(seconds=5),
                endpoint_ping_ewma_ms=260.0,
                conversation_delay_ewma_ms=320.0,
                last_probe_status="healthy",
                last_probe_at=now_at + timedelta(seconds=5),
            ),
            32: _make_runtime_state(
                live_p95_latency_ms=75,
                endpoint_ping_ewma_ms=45.0,
                conversation_delay_ewma_ms=85.0,
                last_probe_status="healthy",
                last_probe_at=now_at + timedelta(seconds=5),
            ),
        }

        with patch(
            "app.services.loadbalancer.planner.get_runtime_states_for_connections",
            AsyncMock(side_effect=[first_state, second_state]),
        ):
            first_plan = await build_attempt_plan(
                db=AsyncMock(),
                profile_id=9,
                model_config=model_config,
                now_at=now_at,
            )

            strategy.routing_policy = make_routing_policy_adaptive(
                deadline_budget_ms=9_000
            )

            second_plan = await build_attempt_plan(
                db=AsyncMock(),
                profile_id=9,
                model_config=model_config,
                now_at=now_at + timedelta(seconds=10),
            )

        assert first_plan.policy.deadline_budget_ms == 30_000
        assert second_plan.policy.deadline_budget_ms == 9_000
        assert [connection.id for connection in first_plan.connections] == [31, 32]
        assert [connection.id for connection in second_plan.connections] == [32, 31]

    @pytest.mark.asyncio
    async def test_get_model_config_with_connections_selects_first_proxy_target_with_live_candidates(
        self,
    ):
        from app.services.loadbalancer.planner import get_model_config_with_connections

        policy = resolve_effective_loadbalance_policy(
            SimpleNamespace(routing_policy=make_routing_policy_adaptive())
        )
        proxy_model = SimpleNamespace(
            profile_id=5,
            model_id="proxy-model",
            model_type="proxy",
            proxy_targets=[
                SimpleNamespace(target_model_id="target-model-a", position=0),
                SimpleNamespace(target_model_id="target-model-b", position=1),
            ],
        )
        target_model_a = SimpleNamespace(
            profile_id=5,
            model_id="target-model-a",
            model_type="native",
        )
        target_model_b = SimpleNamespace(
            profile_id=5,
            model_id="target-model-b",
            model_type="native",
        )
        target_connection = _make_connection(44, priority=0)

        first_result = MagicMock()
        first_result.scalar_one_or_none.return_value = proxy_model
        second_result = MagicMock()
        second_result.scalar_one_or_none.return_value = target_model_a
        third_result = MagicMock()
        third_result.scalar_one_or_none.return_value = target_model_b

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[first_result, second_result, third_result])

        with patch(
            "app.services.loadbalancer.planner.build_attempt_plan",
            AsyncMock(
                side_effect=[
                    AttemptPlan(
                        policy=policy,
                        candidates=[],
                        blocked_connection_ids=[],
                        probe_eligible_connection_ids=[],
                    ),
                    AttemptPlan(
                        policy=policy,
                        candidates=[_make_ranked_candidate(target_connection)],
                        blocked_connection_ids=[],
                        probe_eligible_connection_ids=[],
                    ),
                ]
            ),
        ):
            resolved = await get_model_config_with_connections(
                db=db,
                profile_id=5,
                model_id="proxy-model",
            )

        assert resolved is target_model_b
        assert db.execute.await_count == 3
