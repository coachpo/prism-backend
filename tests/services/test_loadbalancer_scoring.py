from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

from app.models.models import Connection
from app.services.loadbalancer.policy import resolve_effective_loadbalance_policy
from app.services.loadbalancer.types import (
    AttemptCandidateScoreInput,
    RuntimeCircuitState,
)
from tests.loadbalance_strategy_helpers import make_routing_policy_adaptive


def _make_policy(**overrides: object):
    return resolve_effective_loadbalance_policy(
        SimpleNamespace(
            routing_policy=make_routing_policy_adaptive(**cast(Any, overrides))
        )
    )


def _make_candidate_input(
    connection_id: int,
    *,
    priority: int,
    qps_limit: int | None = None,
    max_in_flight_non_stream: int | None = None,
    max_in_flight_stream: int | None = None,
    circuit_state: str = "closed",
    blocked_until_at: datetime | None = None,
    banned_until_at: datetime | None = None,
    probe_available_at: datetime | None = None,
    in_flight_non_stream: int = 0,
    in_flight_stream: int = 0,
    qps_window_count: int = 0,
    live_p95_latency_ms: float | None = 100.0,
    last_live_failure_kind: str | None = None,
    last_live_failure_at: datetime | None = None,
    last_live_success_at: datetime | None = None,
    last_probe_status: str | None = None,
    last_probe_at: datetime | None = None,
    endpoint_ping_ewma_ms: float | None = None,
    conversation_delay_ewma_ms: float | None = None,
) -> AttemptCandidateScoreInput:
    connection = cast(
        Connection,
        cast(
            object,
            SimpleNamespace(
                id=connection_id,
                priority=priority,
                qps_limit=qps_limit,
                max_in_flight_non_stream=max_in_flight_non_stream,
                max_in_flight_stream=max_in_flight_stream,
                model_rollup_latency_ms=1,
                vendor_rollup_latency_ms=1,
            ),
        ),
    )
    return AttemptCandidateScoreInput(
        connection=connection,
        circuit_state=cast(RuntimeCircuitState, circuit_state),
        blocked_until_at=blocked_until_at,
        banned_until_at=banned_until_at,
        probe_available_at=probe_available_at,
        in_flight_non_stream=in_flight_non_stream,
        in_flight_stream=in_flight_stream,
        qps_window_count=qps_window_count,
        live_p95_latency_ms=live_p95_latency_ms,
        last_live_failure_kind=last_live_failure_kind,
        last_live_failure_at=last_live_failure_at,
        last_live_success_at=last_live_success_at,
        last_probe_status=last_probe_status,
        last_probe_at=last_probe_at,
        endpoint_ping_ewma_ms=endpoint_ping_ewma_ms,
        conversation_delay_ewma_ms=conversation_delay_ewma_ms,
    )


class TestLoadbalancerScoring:
    def test_rank_candidates_penalizes_saturation(self):
        from app.services.loadbalancer.scoring import rank_candidates

        now_at = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        policy = _make_policy()
        lightly_loaded = _make_candidate_input(
            11,
            priority=5,
            qps_limit=10,
            max_in_flight_non_stream=10,
            qps_window_count=1,
            in_flight_non_stream=1,
            last_probe_status="healthy",
            last_probe_at=now_at - timedelta(seconds=10),
            endpoint_ping_ewma_ms=110.0,
            conversation_delay_ewma_ms=160.0,
        )
        saturated = _make_candidate_input(
            12,
            priority=0,
            qps_limit=10,
            max_in_flight_non_stream=10,
            qps_window_count=10,
            in_flight_non_stream=10,
            last_probe_status="healthy",
            last_probe_at=now_at - timedelta(seconds=10),
            endpoint_ping_ewma_ms=110.0,
            conversation_delay_ewma_ms=160.0,
        )

        ranked = rank_candidates(
            policy=policy,
            candidate_inputs=[saturated, lightly_loaded],
            now_at=now_at,
        )

        assert [candidate.connection.id for candidate in ranked] == [11, 12]

    def test_rank_candidates_penalizes_recent_live_failures(self):
        from app.services.loadbalancer.scoring import rank_candidates

        now_at = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        policy = _make_policy()
        healthy = _make_candidate_input(
            21,
            priority=1,
            last_probe_status="healthy",
            last_probe_at=now_at - timedelta(seconds=10),
            endpoint_ping_ewma_ms=90.0,
            conversation_delay_ewma_ms=140.0,
        )
        recent_failure = _make_candidate_input(
            22,
            priority=0,
            last_live_failure_kind="timeout",
            last_live_failure_at=now_at - timedelta(seconds=5),
            last_probe_status="healthy",
            last_probe_at=now_at - timedelta(seconds=10),
            endpoint_ping_ewma_ms=90.0,
            conversation_delay_ewma_ms=140.0,
        )

        ranked = rank_candidates(
            policy=policy,
            candidate_inputs=[recent_failure, healthy],
            now_at=now_at,
        )

        assert [candidate.connection.id for candidate in ranked] == [21, 22]

    def test_rank_candidates_uses_endpoint_ping_and_conversation_delay(self):
        from app.services.loadbalancer.scoring import rank_candidates

        now_at = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        policy = _make_policy(endpoint_ping_weight=1.0, conversation_delay_weight=1.0)
        faster_probe = _make_candidate_input(
            31,
            priority=1,
            live_p95_latency_ms=120.0,
            last_probe_status="healthy",
            last_probe_at=now_at - timedelta(seconds=10),
            endpoint_ping_ewma_ms=45.0,
            conversation_delay_ewma_ms=85.0,
        )
        slower_probe = _make_candidate_input(
            32,
            priority=0,
            live_p95_latency_ms=120.0,
            last_probe_status="healthy",
            last_probe_at=now_at - timedelta(seconds=10),
            endpoint_ping_ewma_ms=250.0,
            conversation_delay_ewma_ms=420.0,
        )

        ranked = rank_candidates(
            policy=policy,
            candidate_inputs=[slower_probe, faster_probe],
            now_at=now_at,
        )

        assert [candidate.connection.id for candidate in ranked] == [31, 32]

    def test_rank_candidates_penalizes_stale_monitoring_state(self):
        from app.services.loadbalancer.scoring import rank_candidates

        now_at = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        policy = _make_policy(stale_after_seconds=300)
        fresh = _make_candidate_input(
            41,
            priority=1,
            last_probe_status="healthy",
            last_probe_at=now_at - timedelta(seconds=20),
            endpoint_ping_ewma_ms=80.0,
            conversation_delay_ewma_ms=120.0,
        )
        stale = _make_candidate_input(
            42,
            priority=0,
            last_probe_status="healthy",
            last_probe_at=now_at - timedelta(seconds=600),
            endpoint_ping_ewma_ms=80.0,
            conversation_delay_ewma_ms=120.0,
        )

        ranked = rank_candidates(
            policy=policy,
            candidate_inputs=[stale, fresh],
            now_at=now_at,
        )

        assert [candidate.connection.id for candidate in ranked] == [41, 42]

    def test_rank_candidates_prioritizes_fresh_live_signals_over_conflicting_probe_signals(
        self,
    ):
        from app.services.loadbalancer.scoring import rank_candidates

        now_at = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        policy = _make_policy(endpoint_ping_weight=1.0, conversation_delay_weight=1.0)
        better_live = _make_candidate_input(
            43,
            priority=1,
            live_p95_latency_ms=90.0,
            last_live_success_at=now_at - timedelta(seconds=5),
            last_probe_status="unhealthy",
            last_probe_at=now_at - timedelta(seconds=5),
            endpoint_ping_ewma_ms=320.0,
            conversation_delay_ewma_ms=700.0,
        )
        better_probe_only = _make_candidate_input(
            44,
            priority=0,
            live_p95_latency_ms=150.0,
            last_live_success_at=now_at - timedelta(seconds=5),
            last_probe_status="healthy",
            last_probe_at=now_at - timedelta(seconds=5),
            endpoint_ping_ewma_ms=25.0,
            conversation_delay_ewma_ms=35.0,
        )

        ranked = rank_candidates(
            policy=policy,
            candidate_inputs=[better_probe_only, better_live],
            now_at=now_at,
        )

        assert [candidate.connection.id for candidate in ranked] == [43, 44]

    def test_candidate_sort_key_uses_priority_then_id_as_stable_tie_breaker(self):
        from app.services.loadbalancer.scoring import (
            candidate_sort_key,
            rank_candidates,
        )

        now_at = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        policy = _make_policy(monitoring_enabled=False)
        lower_priority = _make_candidate_input(
            51, priority=0, live_p95_latency_ms=100.0
        )
        higher_priority = _make_candidate_input(
            52, priority=1, live_p95_latency_ms=100.0
        )
        same_priority_lower_id = _make_candidate_input(
            53,
            priority=1,
            live_p95_latency_ms=100.0,
        )
        same_priority_higher_id = _make_candidate_input(
            54,
            priority=1,
            live_p95_latency_ms=100.0,
        )

        assert candidate_sort_key(
            policy, lower_priority, now_at=now_at
        ) < candidate_sort_key(
            policy,
            higher_priority,
            now_at=now_at,
        )
        assert candidate_sort_key(
            policy,
            same_priority_lower_id,
            now_at=now_at,
        ) < candidate_sort_key(
            policy,
            same_priority_higher_id,
            now_at=now_at,
        )

        ranked = rank_candidates(
            policy=policy,
            candidate_inputs=[same_priority_higher_id, lower_priority, higher_priority],
            now_at=now_at,
        )

        assert [candidate.connection.id for candidate in ranked] == [51, 52, 54]
