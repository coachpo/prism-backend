from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.loadbalance_strategy_helpers import make_loadbalance_strategy


class TestLoadbalancerPlanner:
    def test_get_active_connections_sorts_active_connections_by_priority_then_id(self):
        from app.models.models import Connection, Endpoint, ModelConfig
        from app.services.loadbalancer.planner import get_active_connections

        endpoint = Endpoint(
            id=1,
            profile_id=1,
            name="endpoint",
            base_url="https://example.com",
            api_key="sk-test",
            position=0,
        )
        inactive = Connection(
            id=4,
            profile_id=1,
            model_config_id=1,
            endpoint_id=1,
            priority=0,
            is_active=False,
        )
        low_id = Connection(
            id=7,
            profile_id=1,
            model_config_id=1,
            endpoint_id=1,
            priority=0,
            is_active=True,
        )
        high_id = Connection(
            id=9,
            profile_id=1,
            model_config_id=1,
            endpoint_id=1,
            priority=0,
            is_active=True,
        )
        later_priority = Connection(
            id=11,
            profile_id=1,
            model_config_id=1,
            endpoint_id=1,
            priority=1,
            is_active=True,
        )

        for connection in (inactive, low_id, high_id, later_priority):
            connection.endpoint_rel = endpoint

        model_config = ModelConfig(
            id=1,
            profile_id=1,
            provider_id=1,
            model_id="gpt-test",
            model_type="native",
            loadbalance_strategy=make_loadbalance_strategy(
                profile_id=1,
                strategy_type="failover",
            ),
            is_enabled=True,
            connections=[later_priority, inactive, high_id, low_id],
        )

        ordered = get_active_connections(model_config)

        assert [connection.id for connection in ordered] == [7, 9, 11]

    @pytest.mark.asyncio
    async def test_get_model_config_with_connections_selects_first_available_proxy_target(
        self,
    ):
        from app.models.models import Connection
        from app.services.loadbalancer.planner import get_model_config_with_connections
        from app.services.loadbalancer.types import AttemptPlan

        proxy_model = SimpleNamespace(
            profile_id=5,
            model_id="alias-model",
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
        connection = SimpleNamespace(id=11)

        first_result = MagicMock()
        first_result.scalar_one_or_none.return_value = proxy_model
        second_result = MagicMock()
        second_result.scalar_one_or_none.return_value = target_model_a

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[first_result, second_result])

        with patch(
            "app.services.loadbalancer.planner.build_attempt_plan",
            AsyncMock(
                return_value=AttemptPlan(
                    connections=cast(list[Connection], [connection]),
                    blocked_connection_ids=[],
                    probe_eligible_connection_ids=[],
                )
            ),
        ):
            resolved = await get_model_config_with_connections(
                db=db,
                profile_id=5,
                model_id="alias-model",
            )

        assert resolved is target_model_a
        assert db.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_get_model_config_with_connections_skips_proxy_target_without_attempt_plan(
        self,
    ):
        from app.models.models import Connection
        from app.services.loadbalancer.planner import get_model_config_with_connections
        from app.services.loadbalancer.types import AttemptPlan

        proxy_model = SimpleNamespace(
            profile_id=5,
            model_id="alias-model",
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
                        connections=[],
                        blocked_connection_ids=[],
                        probe_eligible_connection_ids=[],
                    ),
                    AttemptPlan(
                        connections=cast(list[Connection], [SimpleNamespace(id=22)]),
                        blocked_connection_ids=[],
                        probe_eligible_connection_ids=[],
                    ),
                ]
            ),
        ):
            resolved = await get_model_config_with_connections(
                db=db,
                profile_id=5,
                model_id="alias-model",
            )

        assert resolved is target_model_b
        assert db.execute.await_count == 3

    @pytest.mark.asyncio
    async def test_build_attempt_plan_reports_probe_eligible_candidates_without_mutation(
        self,
    ):
        from app.models.models import ModelConfig
        from app.services.loadbalancer.planner import build_attempt_plan

        connection = SimpleNamespace(
            id=7,
            priority=0,
            health_status="healthy",
            is_active=True,
            endpoint_rel=object(),
            endpoint_id=12,
        )
        model_config = cast(
            ModelConfig,
            cast(
                object,
                SimpleNamespace(
                    connections=[connection],
                    loadbalance_strategy=SimpleNamespace(
                        strategy_type="failover",
                        failover_recovery_enabled=True,
                    ),
                    model_id="gpt-4o-mini",
                    provider_id=1,
                ),
            ),
        )
        current_state = SimpleNamespace(
            blocked_until_at=None,
            probe_eligible_logged=False,
        )

        with patch(
            "app.services.loadbalancer.planner.get_current_states_for_connections",
            AsyncMock(return_value={7: current_state}),
        ):
            plan = await build_attempt_plan(
                db=AsyncMock(),
                profile_id=5,
                model_config=model_config,
                now_at=None,
            )

        assert plan.connections == [connection]
        assert plan.blocked_connection_ids == []
        assert plan.probe_eligible_connection_ids == []

    @pytest.mark.asyncio
    async def test_build_attempt_plan_reports_expired_block_as_probe_eligible_candidate(
        self,
    ):
        from datetime import datetime, timezone

        from app.models.models import ModelConfig
        from app.services.loadbalancer.planner import build_attempt_plan

        connection = SimpleNamespace(
            id=7,
            priority=0,
            health_status="healthy",
            is_active=True,
            endpoint_rel=object(),
            endpoint_id=12,
        )
        model_config = cast(
            ModelConfig,
            cast(
                object,
                SimpleNamespace(
                    connections=[connection],
                    loadbalance_strategy=SimpleNamespace(
                        strategy_type="failover",
                        failover_recovery_enabled=True,
                    ),
                    model_id="gpt-4o-mini",
                    provider_id=1,
                ),
            ),
        )
        current_state = SimpleNamespace(
            blocked_until_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            probe_eligible_logged=False,
        )

        with patch(
            "app.services.loadbalancer.planner.get_current_states_for_connections",
            AsyncMock(return_value={7: current_state}),
        ):
            plan = await build_attempt_plan(
                db=AsyncMock(),
                profile_id=5,
                model_config=model_config,
                now_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
            )

        assert plan.connections == [connection]
        assert plan.blocked_connection_ids == []
        assert plan.probe_eligible_connection_ids == [7]
