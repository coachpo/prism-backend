from types import SimpleNamespace
from typing import cast
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, text

from app.core.database import AsyncSessionLocal
from app.models.models import (
    Connection,
    Endpoint,
    ModelConfig,
    ModelProxyTarget,
    Profile,
    Vendor,
)

from tests.loadbalance_strategy_helpers import make_loadbalance_strategy


def _vendor_key_for_api_family(api_family: str) -> str:
    return "google" if api_family == "gemini" else api_family


async def _get_or_create_vendor(db, *, api_family: str = "openai"):
    vendor_key = _vendor_key_for_api_family(api_family)
    vendor = (
        (
            await db.execute(
                select(Vendor)
                .where(Vendor.key == vendor_key)
                .order_by(Vendor.id.asc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if vendor is not None:
        return vendor

    vendor = Vendor(key=vendor_key, name="OpenAI planner")
    db.add(vendor)
    await db.flush()
    return vendor


class TestLoadbalancerPlanner:
    @pytest.mark.asyncio
    async def test_round_robin_cursor_state_table_exists_as_unlogged(self):
        async with AsyncSessionLocal() as db:
            regclass = (
                await db.execute(
                    text("SELECT to_regclass('public.loadbalance_round_robin_state')")
                )
            ).scalar_one()

            persistence = None
            if regclass is not None:
                persistence = (
                    await db.execute(
                        text(
                            "SELECT relpersistence FROM pg_class WHERE oid = 'loadbalance_round_robin_state'::regclass"
                        )
                    )
                ).scalar_one()

        assert regclass == "loadbalance_round_robin_state"
        assert persistence in {"u", b"u"}

    def test_get_active_connections_sorts_active_connections_by_priority_then_id(self):
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
            vendor_id=1,
            api_family="openai",
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
    async def test_build_attempt_plan_fill_first_preserves_priority_order_while_failover_remains_health_aware(
        self,
    ):
        from app.models.models import ModelConfig
        from app.services.loadbalancer.planner import build_attempt_plan

        unhealthy_primary = SimpleNamespace(
            id=7,
            priority=0,
            health_status="unhealthy",
            is_active=True,
            endpoint_rel=object(),
            endpoint_id=12,
        )
        healthy_lower_id = SimpleNamespace(
            id=11,
            priority=1,
            health_status="healthy",
            is_active=True,
            endpoint_rel=object(),
            endpoint_id=13,
        )
        healthy_higher_id = SimpleNamespace(
            id=13,
            priority=1,
            health_status="healthy",
            is_active=True,
            endpoint_rel=object(),
            endpoint_id=14,
        )

        fill_first_model_config = cast(
            ModelConfig,
            cast(
                object,
                SimpleNamespace(
                    connections=[
                        healthy_higher_id,
                        unhealthy_primary,
                        healthy_lower_id,
                    ],
                    loadbalance_strategy=SimpleNamespace(
                        strategy_type="fill-first",
                        failover_recovery_enabled=False,
                    ),
                    model_id="gpt-4o-mini",
                    vendor_id=1,
                ),
            ),
        )
        failover_model_config = cast(
            ModelConfig,
            cast(
                object,
                SimpleNamespace(
                    connections=[
                        healthy_higher_id,
                        unhealthy_primary,
                        healthy_lower_id,
                    ],
                    loadbalance_strategy=SimpleNamespace(
                        strategy_type="failover",
                        failover_recovery_enabled=False,
                    ),
                    model_id="gpt-4o-mini",
                    vendor_id=1,
                ),
            ),
        )

        fill_first_plan = await build_attempt_plan(
            db=AsyncMock(),
            profile_id=5,
            model_config=fill_first_model_config,
            now_at=None,
        )
        failover_plan = await build_attempt_plan(
            db=AsyncMock(),
            profile_id=5,
            model_config=failover_model_config,
            now_at=None,
        )

        assert [connection.id for connection in fill_first_plan.connections] == [
            7,
            11,
            13,
        ]
        assert [connection.id for connection in failover_plan.connections] == [
            11,
            13,
            7,
        ]

    @pytest.mark.asyncio
    async def test_build_attempt_plan_round_robin_rotates_priority_order_between_calls(
        self,
    ):
        from app.models.models import ModelConfig
        from app.services.loadbalancer.planner import build_attempt_plan

        primary = SimpleNamespace(
            id=7,
            priority=0,
            health_status="healthy",
            is_active=True,
            endpoint_rel=object(),
            endpoint_id=12,
        )
        secondary = SimpleNamespace(
            id=11,
            priority=1,
            health_status="healthy",
            is_active=True,
            endpoint_rel=object(),
            endpoint_id=13,
        )
        tertiary = SimpleNamespace(
            id=13,
            priority=2,
            health_status="healthy",
            is_active=True,
            endpoint_rel=object(),
            endpoint_id=14,
        )

        model_config = cast(
            ModelConfig,
            cast(
                object,
                SimpleNamespace(
                    id=101,
                    connections=[tertiary, primary, secondary],
                    loadbalance_strategy=SimpleNamespace(
                        strategy_type="round-robin",
                        failover_recovery_enabled=False,
                    ),
                    model_id="gpt-4o-mini",
                    vendor_id=1,
                ),
            ),
        )

        with patch(
            "app.services.loadbalancer.planner.claim_round_robin_cursor_position",
            AsyncMock(side_effect=[0, 1, 2]),
        ):
            first_plan = await build_attempt_plan(
                db=AsyncMock(),
                profile_id=5,
                model_config=model_config,
                now_at=None,
            )
            second_plan = await build_attempt_plan(
                db=AsyncMock(),
                profile_id=5,
                model_config=model_config,
                now_at=None,
            )
            third_plan = await build_attempt_plan(
                db=AsyncMock(),
                profile_id=5,
                model_config=model_config,
                now_at=None,
            )

        assert [connection.id for connection in first_plan.connections] == [7, 11, 13]

    @pytest.mark.asyncio
    async def test_round_robin_cursor_is_persisted_in_db_and_proxy_preview_does_not_advance_it(
        self,
    ):
        from app.services.loadbalancer.planner import (
            build_attempt_plan,
            get_model_config_with_connections,
        )

        suffix = uuid4().hex[:8]

        async with AsyncSessionLocal() as session:
            vendor = await _get_or_create_vendor(session)
            profile = Profile(
                name=f"Round Robin Planner {suffix}", is_active=False, version=0
            )
            strategy = make_loadbalance_strategy(
                profile=profile,
                strategy_type="round-robin",
                name=f"round-robin-{suffix}",
            )
            native_model = ModelConfig(
                vendor=vendor,
                profile=profile,
                api_family="openai",
                model_id=f"native-{suffix}",
                model_type="native",
                loadbalance_strategy=strategy,
                is_enabled=True,
            )
            proxy_model = ModelConfig(
                vendor=vendor,
                profile=profile,
                api_family="openai",
                model_id=f"proxy-{suffix}",
                model_type="proxy",
                is_enabled=True,
            )
            endpoint_primary = Endpoint(
                profile=profile,
                name=f"endpoint-primary-{suffix}",
                base_url="https://primary.example.com/v1",
                api_key="sk-primary",
                position=0,
            )
            endpoint_secondary = Endpoint(
                profile=profile,
                name=f"endpoint-secondary-{suffix}",
                base_url="https://secondary.example.com/v1",
                api_key="sk-secondary",
                position=1,
            )
            endpoint_tertiary = Endpoint(
                profile=profile,
                name=f"endpoint-tertiary-{suffix}",
                base_url="https://tertiary.example.com/v1",
                api_key="sk-tertiary",
                position=2,
            )
            session.add_all(
                [
                    profile,
                    strategy,
                    native_model,
                    proxy_model,
                    endpoint_primary,
                    endpoint_secondary,
                    endpoint_tertiary,
                ]
            )
            await session.flush()

            session.add_all(
                [
                    Connection(
                        profile=profile,
                        model_config_rel=native_model,
                        endpoint_rel=endpoint_primary,
                        is_active=True,
                        priority=0,
                        name="primary",
                    ),
                    Connection(
                        profile=profile,
                        model_config_rel=native_model,
                        endpoint_rel=endpoint_secondary,
                        is_active=True,
                        priority=1,
                        name="secondary",
                    ),
                    Connection(
                        profile=profile,
                        model_config_rel=native_model,
                        endpoint_rel=endpoint_tertiary,
                        is_active=True,
                        priority=2,
                        name="tertiary",
                    ),
                    ModelProxyTarget(
                        source_model_config=proxy_model,
                        target_model_config=native_model,
                        position=0,
                    ),
                ]
            )
            await session.commit()

            profile_id = profile.id
            native_model_id = native_model.id
            native_model_name = native_model.model_id
            proxy_model_name = proxy_model.model_id

        async with AsyncSessionLocal() as session:
            resolved = await get_model_config_with_connections(
                session,
                profile_id,
                proxy_model_name,
            )
            preview_cursor_row = None
            cursor_table_exists = (
                await session.execute(
                    text("SELECT to_regclass('public.loadbalance_round_robin_state')")
                )
            ).scalar_one()
            if cursor_table_exists is not None:
                preview_cursor_row = (
                    (
                        await session.execute(
                            text(
                                "SELECT next_cursor FROM loadbalance_round_robin_state "
                                "WHERE profile_id = :profile_id AND model_config_id = :model_config_id"
                            ),
                            {
                                "profile_id": profile_id,
                                "model_config_id": native_model_id,
                            },
                        )
                    )
                    .mappings()
                    .first()
                )

            assert resolved is not None
            assert preview_cursor_row is None

            first_plan = await build_attempt_plan(
                session,
                profile_id,
                resolved,
                None,
            )

            first_cursor_row = None
            if cursor_table_exists is not None:
                first_cursor_row = (
                    (
                        await session.execute(
                            text(
                                "SELECT next_cursor FROM loadbalance_round_robin_state "
                                "WHERE profile_id = :profile_id AND model_config_id = :model_config_id"
                            ),
                            {
                                "profile_id": profile_id,
                                "model_config_id": native_model_id,
                            },
                        )
                    )
                    .mappings()
                    .first()
                )

        assert [connection.priority for connection in first_plan.connections] == [
            0,
            1,
            2,
        ]
        assert first_cursor_row is not None
        assert first_cursor_row["next_cursor"] == 1

        async with AsyncSessionLocal() as session:
            resolved_native = await get_model_config_with_connections(
                session,
                profile_id,
                native_model_name,
            )
            assert resolved_native is not None
            second_plan = await build_attempt_plan(
                session,
                profile_id,
                resolved_native,
                None,
            )
            second_cursor_row = (
                (
                    await session.execute(
                        text(
                            "SELECT next_cursor FROM loadbalance_round_robin_state "
                            "WHERE profile_id = :profile_id AND model_config_id = :model_config_id"
                        ),
                        {"profile_id": profile_id, "model_config_id": native_model_id},
                    )
                )
                .mappings()
                .first()
            )

        async with AsyncSessionLocal() as session:
            resolved_third = await get_model_config_with_connections(
                session,
                profile_id,
                native_model_name,
            )
            assert resolved_third is not None
            third_plan = await build_attempt_plan(
                session,
                profile_id,
                resolved_third,
                None,
            )
            third_cursor_row = (
                (
                    await session.execute(
                        text(
                            "SELECT next_cursor FROM loadbalance_round_robin_state "
                            "WHERE profile_id = :profile_id AND model_config_id = :model_config_id"
                        ),
                        {"profile_id": profile_id, "model_config_id": native_model_id},
                    )
                )
                .mappings()
                .first()
            )

        assert [connection.priority for connection in second_plan.connections] == [
            1,
            2,
            0,
        ]
        assert second_cursor_row is not None
        assert second_cursor_row["next_cursor"] == 2
        assert [connection.priority for connection in third_plan.connections] == [
            2,
            0,
            1,
        ]
        assert third_cursor_row is not None
        assert third_cursor_row["next_cursor"] == 0

    @pytest.mark.asyncio
    async def test_build_attempt_plan_fill_first_with_recovery_filters_blocked_connections_preserving_priority_order(
        self,
    ):
        from datetime import datetime, timezone

        from app.models.models import ModelConfig
        from app.services.loadbalancer.planner import build_attempt_plan

        blocked_primary = SimpleNamespace(
            id=21,
            priority=0,
            health_status="healthy",
            is_active=True,
            endpoint_rel=object(),
            endpoint_id=31,
        )
        unhealthy_secondary = SimpleNamespace(
            id=22,
            priority=1,
            health_status="unhealthy",
            is_active=True,
            endpoint_rel=object(),
            endpoint_id=32,
        )
        probe_eligible_tertiary = SimpleNamespace(
            id=23,
            priority=2,
            health_status="healthy",
            is_active=True,
            endpoint_rel=object(),
            endpoint_id=33,
        )
        model_config = cast(
            ModelConfig,
            cast(
                object,
                SimpleNamespace(
                    connections=[
                        probe_eligible_tertiary,
                        unhealthy_secondary,
                        blocked_primary,
                    ],
                    loadbalance_strategy=SimpleNamespace(
                        strategy_type="fill-first",
                        failover_recovery_enabled=True,
                    ),
                    model_id="gpt-4o-mini",
                    vendor_id=1,
                ),
            ),
        )
        now_at = datetime(2026, 3, 26, 12, 0, tzinfo=timezone.utc)
        state_by_connection_id = {
            21: SimpleNamespace(
                blocked_until_at=datetime(2026, 3, 26, 12, 5, tzinfo=timezone.utc),
                probe_eligible_logged=False,
            ),
            23: SimpleNamespace(
                blocked_until_at=datetime(2026, 3, 26, 11, 55, tzinfo=timezone.utc),
                probe_eligible_logged=False,
            ),
        }

        with patch(
            "app.services.loadbalancer.planner.get_current_states_for_connections",
            AsyncMock(return_value=state_by_connection_id),
        ):
            plan = await build_attempt_plan(
                db=AsyncMock(),
                profile_id=5,
                model_config=model_config,
                now_at=now_at,
            )

        assert [connection.id for connection in plan.connections] == [22, 23]
        assert plan.blocked_connection_ids == [21]
        assert plan.probe_eligible_connection_ids == [23]

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
    async def test_get_model_config_with_connections_does_not_advance_round_robin_cursor_for_proxy_preview(
        self,
    ):
        from app.models.models import ModelConfig
        from app.services.loadbalancer.planner import (
            build_attempt_plan,
            get_model_config_with_connections,
        )

        primary = SimpleNamespace(
            id=7,
            priority=0,
            health_status="healthy",
            is_active=True,
            endpoint_rel=object(),
            endpoint_id=12,
        )
        secondary = SimpleNamespace(
            id=11,
            priority=1,
            health_status="healthy",
            is_active=True,
            endpoint_rel=object(),
            endpoint_id=13,
        )
        tertiary = SimpleNamespace(
            id=13,
            priority=2,
            health_status="healthy",
            is_active=True,
            endpoint_rel=object(),
            endpoint_id=14,
        )
        proxy_model = SimpleNamespace(
            profile_id=5,
            model_id="alias-model",
            model_type="proxy",
            proxy_targets=[
                SimpleNamespace(target_model_id="target-model-a", position=0)
            ],
        )
        target_model = cast(
            ModelConfig,
            cast(
                object,
                SimpleNamespace(
                    id=303,
                    profile_id=5,
                    model_id="target-model-a",
                    model_type="native",
                    connections=[tertiary, primary, secondary],
                    loadbalance_strategy=SimpleNamespace(
                        strategy_type="round-robin",
                        failover_recovery_enabled=False,
                    ),
                    vendor_id=1,
                ),
            ),
        )

        first_result = MagicMock()
        first_result.scalar_one_or_none.return_value = proxy_model
        second_result = MagicMock()
        second_result.scalar_one_or_none.return_value = target_model

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[first_result, second_result])

        resolved = await get_model_config_with_connections(
            db=db,
            profile_id=5,
            model_id="alias-model",
        )
        assert resolved is not None
        with patch(
            "app.services.loadbalancer.planner.claim_round_robin_cursor_position",
            AsyncMock(return_value=0),
        ):
            first_real_plan = await build_attempt_plan(
                db=AsyncMock(),
                profile_id=5,
                model_config=resolved,
                now_at=None,
            )

        assert resolved is target_model
        assert [connection.id for connection in first_real_plan.connections] == [
            7,
            11,
            13,
        ]

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
                    vendor_id=1,
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
                    vendor_id=1,
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
