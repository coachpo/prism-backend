from uuid import uuid4

import pytest

from app.core.database import AsyncSessionLocal
from app.models.models import (
    Connection,
    Endpoint,
    LoadbalanceCurrentState,
    ModelConfig,
    Profile,
    Vendor,
)
from tests.loadbalance_strategy_helpers import make_loadbalance_strategy


class TestLoadbalancerState:
    @pytest.mark.asyncio
    async def test_get_current_states_for_connections_returns_empty_for_empty_input(
        self,
    ):
        from app.services.loadbalancer.state import get_current_states_for_connections

        async with AsyncSessionLocal() as db:
            rows = await get_current_states_for_connections(
                db,
                profile_id=1,
                connection_ids=[],
            )

        assert rows == {}

    @pytest.mark.asyncio
    async def test_list_current_states_for_model_orders_by_priority_and_filters_profile(
        self,
    ):
        from app.services.loadbalancer.state import list_current_states_for_model

        suffix = uuid4().hex[:8]

        async with AsyncSessionLocal() as session:
            vendor = Vendor(
                key=f"openai-state-{suffix}",
                name=f"OpenAI {suffix}",
                audit_enabled=False,
                audit_capture_bodies=False,
            )
            profile_one = Profile(
                name=f"Profile One {suffix}", is_active=False, version=0
            )
            profile_two = Profile(
                name=f"Profile Two {suffix}", is_active=False, version=0
            )
            model_one = ModelConfig(
                vendor=vendor,
                profile=profile_one,
                api_family="openai",
                model_id=f"model-one-{suffix}",
                model_type="native",
                loadbalance_strategy=make_loadbalance_strategy(
                    profile=profile_one,
                    strategy_type="failover",
                ),
                is_enabled=True,
            )
            model_two = ModelConfig(
                vendor=vendor,
                profile=profile_two,
                api_family="openai",
                model_id=f"model-two-{suffix}",
                model_type="native",
                loadbalance_strategy=make_loadbalance_strategy(
                    profile=profile_two,
                    strategy_type="failover",
                ),
                is_enabled=True,
            )
            endpoint_one = Endpoint(
                profile=profile_one,
                name=f"endpoint-one-{suffix}",
                base_url="https://one.example.com/v1",
                api_key="sk-one",
                position=0,
            )
            endpoint_two = Endpoint(
                profile=profile_one,
                name=f"endpoint-two-{suffix}",
                base_url="https://two.example.com/v1",
                api_key="sk-two",
                position=1,
            )
            other_endpoint = Endpoint(
                profile=profile_two,
                name=f"endpoint-other-{suffix}",
                base_url="https://other.example.com/v1",
                api_key="sk-other",
                position=0,
            )
            connection_later = Connection(
                profile=profile_one,
                model_config_rel=model_one,
                endpoint_rel=endpoint_two,
                is_active=True,
                priority=1,
                name="later",
            )
            connection_first = Connection(
                profile=profile_one,
                model_config_rel=model_one,
                endpoint_rel=endpoint_one,
                is_active=True,
                priority=0,
                name="first",
            )
            other_connection = Connection(
                profile=profile_two,
                model_config_rel=model_two,
                endpoint_rel=other_endpoint,
                is_active=True,
                priority=0,
                name="other",
            )

            session.add_all(
                [
                    vendor,
                    profile_one,
                    profile_two,
                    model_one,
                    model_two,
                    endpoint_one,
                    endpoint_two,
                    other_endpoint,
                    connection_later,
                    connection_first,
                    other_connection,
                ]
            )
            await session.commit()
            await session.refresh(connection_first)
            await session.refresh(connection_later)
            await session.refresh(other_connection)

            session.add_all(
                [
                    LoadbalanceCurrentState(
                        profile_id=profile_one.id,
                        connection_id=connection_later.id,
                        consecutive_failures=3,
                        last_failure_kind="timeout",
                        last_cooldown_seconds=60.0,
                        probe_eligible_logged=False,
                    ),
                    LoadbalanceCurrentState(
                        profile_id=profile_one.id,
                        connection_id=connection_first.id,
                        consecutive_failures=1,
                        last_failure_kind="transient_http",
                        last_cooldown_seconds=0.0,
                        probe_eligible_logged=False,
                    ),
                    LoadbalanceCurrentState(
                        profile_id=profile_two.id,
                        connection_id=other_connection.id,
                        consecutive_failures=2,
                        last_failure_kind="auth_like",
                        last_cooldown_seconds=120.0,
                        probe_eligible_logged=True,
                    ),
                ]
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            rows = await list_current_states_for_model(
                session,
                profile_id=profile_one.id,
                model_config_id=model_one.id,
            )

        assert [row.connection_id for row in rows] == [
            connection_first.id,
            connection_later.id,
        ]
