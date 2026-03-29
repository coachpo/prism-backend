from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.database import AsyncSessionLocal
from app.models.models import (
    Connection,
    Endpoint,
    LoadbalanceStrategy,
    ModelConfig,
    Profile,
    RoutingConnectionRuntimeLease,
    RoutingConnectionRuntimeState,
    Vendor,
)
from tests.loadbalance_strategy_helpers import make_routing_policy_adaptive


async def _create_connection_fixture(
    *,
    qps_limit: int | None = None,
    max_in_flight_non_stream: int | None = None,
    max_in_flight_stream: int | None = None,
) -> tuple[int, int]:
    suffix = uuid4().hex[:8]

    async with AsyncSessionLocal() as session:
        vendor = Vendor(
            key=f"openai-runtime-store-{suffix}",
            name=f"Runtime Store Vendor {suffix}",
            audit_enabled=False,
            audit_capture_bodies=False,
        )
        profile = Profile(
            name=f"Runtime Store Profile {suffix}",
            is_active=False,
            version=0,
        )
        endpoint = Endpoint(
            profile=profile,
            name=f"endpoint-{suffix}",
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            position=0,
        )
        model = ModelConfig(
            vendor=vendor,
            profile=profile,
            api_family="openai",
            model_id=f"model-{suffix}",
            model_type="native",
            loadbalance_strategy=LoadbalanceStrategy(
                profile=profile,
                name=f"runtime-store-strategy-{suffix}",
                routing_policy=make_routing_policy_adaptive(),
            ),
            is_enabled=True,
        )
        connection = Connection(
            profile=profile,
            model_config_rel=model,
            endpoint_rel=endpoint,
            is_active=True,
            priority=0,
            qps_limit=qps_limit,
            max_in_flight_non_stream=max_in_flight_non_stream,
            max_in_flight_stream=max_in_flight_stream,
            name=f"connection-{suffix}",
        )
        session.add_all([vendor, profile, endpoint, model, connection])
        await session.commit()
        await session.refresh(profile)
        await session.refresh(connection)
        return profile.id, connection.id


async def _load_connection(connection_id: int) -> Connection:
    async with AsyncSessionLocal() as session:
        connection = (
            await session.execute(
                select(Connection).where(Connection.id == connection_id)
            )
        ).scalar_one()
        return connection


class TestLoadbalancerRuntimeStore:
    @pytest.mark.asyncio
    async def test_upsert_and_lock_runtime_state_reuses_single_row_per_connection(self):
        from app.services.loadbalancer.runtime_store import (
            upsert_and_lock_runtime_state,
        )

        profile_id, connection_id = await _create_connection_fixture()
        now_at = datetime(2026, 3, 29, 9, 0, tzinfo=timezone.utc)

        async with AsyncSessionLocal() as session:
            state_row = await upsert_and_lock_runtime_state(
                session=session,
                profile_id=profile_id,
                connection_id=connection_id,
                now_at=now_at,
            )
            state_row.last_probe_status = "healthy"
            await session.commit()
            first_row_id = state_row.id

        async with AsyncSessionLocal() as session:
            state_row = await upsert_and_lock_runtime_state(
                session=session,
                profile_id=profile_id,
                connection_id=connection_id,
                now_at=now_at + timedelta(seconds=5),
            )
            assert state_row.id == first_row_id
            assert state_row.last_probe_status == "healthy"
            await session.rollback()

        async with AsyncSessionLocal() as session:
            row_count = await session.scalar(
                select(func.count())
                .select_from(RoutingConnectionRuntimeState)
                .where(
                    RoutingConnectionRuntimeState.profile_id == profile_id,
                    RoutingConnectionRuntimeState.connection_id == connection_id,
                )
            )

        assert row_count == 1

    @pytest.mark.asyncio
    async def test_acquire_and_release_normal_attempt_leases_update_runtime_counters(
        self,
    ):
        from app.services.loadbalancer.runtime_store import (
            acquire_connection_lease,
            release_connection_lease,
        )

        profile_id, connection_id = await _create_connection_fixture(
            max_in_flight_non_stream=1,
        )
        connection = await _load_connection(connection_id)
        start = datetime(2026, 3, 29, 10, 0, tzinfo=timezone.utc)

        async with AsyncSessionLocal() as session:
            acquired = await acquire_connection_lease(
                session=session,
                profile_id=profile_id,
                connection=connection,
                lease_kind="non_stream",
                lease_ttl_seconds=30,
                now_at=start,
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            blocked = await acquire_connection_lease(
                session=session,
                profile_id=profile_id,
                connection=connection,
                lease_kind="non_stream",
                lease_ttl_seconds=30,
                now_at=start,
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            released = await release_connection_lease(
                session=session,
                profile_id=profile_id,
                lease_token=acquired.lease_token,
                now_at=start + timedelta(seconds=1),
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            reacquired = await acquire_connection_lease(
                session=session,
                profile_id=profile_id,
                connection=connection,
                lease_kind="non_stream",
                lease_ttl_seconds=30,
                now_at=start + timedelta(seconds=2),
            )
            await session.commit()

        assert acquired.admitted is True
        assert acquired.lease_token is not None
        assert blocked.admitted is False
        assert blocked.deny_reason == "in_flight_limit"
        assert released is True
        assert reacquired.admitted is True

    @pytest.mark.asyncio
    async def test_heartbeat_connection_lease_keeps_stream_slot_reserved_until_extended_expiry(
        self,
    ):
        from app.services.loadbalancer.runtime_store import (
            acquire_connection_lease,
            heartbeat_connection_lease,
        )

        profile_id, connection_id = await _create_connection_fixture(
            max_in_flight_stream=1,
        )
        connection = await _load_connection(connection_id)
        start = datetime(2026, 3, 29, 11, 0, tzinfo=timezone.utc)

        async with AsyncSessionLocal() as session:
            acquired = await acquire_connection_lease(
                session=session,
                profile_id=profile_id,
                connection=connection,
                lease_kind="stream",
                lease_ttl_seconds=5,
                now_at=start,
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            heartbeated = await heartbeat_connection_lease(
                session=session,
                profile_id=profile_id,
                lease_token=acquired.lease_token,
                lease_ttl_seconds=5,
                now_at=start + timedelta(seconds=4),
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            blocked = await acquire_connection_lease(
                session=session,
                profile_id=profile_id,
                connection=connection,
                lease_kind="stream",
                lease_ttl_seconds=5,
                now_at=start + timedelta(seconds=6),
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            reacquired = await acquire_connection_lease(
                session=session,
                profile_id=profile_id,
                connection=connection,
                lease_kind="stream",
                lease_ttl_seconds=5,
                now_at=start + timedelta(seconds=10),
            )
            await session.commit()

        assert acquired.admitted is True
        assert acquired.lease_token is not None
        assert heartbeated is True
        assert blocked.admitted is False
        assert blocked.deny_reason == "in_flight_limit"
        assert reacquired.admitted is True

    @pytest.mark.asyncio
    async def test_acquire_half_open_probe_lease_is_single_flight_when_probe_is_ready(
        self,
    ):
        from app.services.loadbalancer.runtime_store import (
            acquire_half_open_probe_lease,
            record_connection_failure_state,
        )

        profile_id, connection_id = await _create_connection_fixture()
        start = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)

        async with AsyncSessionLocal() as session:
            _ = await record_connection_failure_state(
                session=session,
                profile_id=profile_id,
                connection_id=connection_id,
                failure_kind="timeout",
                cooldown_seconds=30.0,
                strike_incremented=False,
                ban_mode="off",
                ban_duration_seconds=0,
                now_at=start,
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            first = await acquire_half_open_probe_lease(
                session=session,
                profile_id=profile_id,
                connection_id=connection_id,
                lease_ttl_seconds=10,
                now_at=start + timedelta(seconds=31),
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            second = await acquire_half_open_probe_lease(
                session=session,
                profile_id=profile_id,
                connection_id=connection_id,
                lease_ttl_seconds=10,
                now_at=start + timedelta(seconds=31),
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            state_row = (
                await session.execute(
                    select(RoutingConnectionRuntimeState).where(
                        RoutingConnectionRuntimeState.profile_id == profile_id,
                        RoutingConnectionRuntimeState.connection_id == connection_id,
                    )
                )
            ).scalar_one()

        assert first.admitted is True
        assert first.lease_token is not None
        assert second.admitted is False
        assert second.deny_reason == "probe_in_progress"
        assert state_row.circuit_state == "half_open"

    @pytest.mark.asyncio
    async def test_apply_fused_monitoring_update_records_synthetic_monitoring_fields(
        self,
    ):
        from app.services.loadbalancer.runtime_store import (
            apply_fused_monitoring_update,
        )

        profile_id, connection_id = await _create_connection_fixture()
        checked_at = datetime(2026, 3, 29, 13, 0, tzinfo=timezone.utc)

        async with AsyncSessionLocal() as session:
            _ = await apply_fused_monitoring_update(
                session=session,
                profile_id=profile_id,
                connection_id=connection_id,
                last_probe_status="healthy",
                last_probe_at=checked_at,
                endpoint_ping_ewma_ms=123.5,
                conversation_delay_ewma_ms=456.5,
                live_p95_latency_ms=None,
                last_live_failure_kind=None,
                last_live_failure_at=None,
                last_live_success_at=checked_at,
                now_at=checked_at,
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            state_row = (
                await session.execute(
                    select(RoutingConnectionRuntimeState).where(
                        RoutingConnectionRuntimeState.profile_id == profile_id,
                        RoutingConnectionRuntimeState.connection_id == connection_id,
                    )
                )
            ).scalar_one()

        assert state_row.last_probe_status == "healthy"
        assert state_row.last_probe_at == checked_at
        assert state_row.endpoint_ping_ewma_ms is not None
        assert state_row.conversation_delay_ewma_ms is not None
        assert float(state_row.endpoint_ping_ewma_ms) == pytest.approx(123.5)
        assert float(state_row.conversation_delay_ewma_ms) == pytest.approx(456.5)
        assert state_row.last_live_success_at == checked_at

    @pytest.mark.asyncio
    async def test_reconcile_connection_runtime_state_expires_abandoned_leases_and_compacts_empty_rows(
        self,
    ):
        from app.services.loadbalancer.runtime_store import (
            acquire_connection_lease,
            reconcile_connection_runtime_state,
        )

        profile_id, connection_id = await _create_connection_fixture(
            max_in_flight_non_stream=1,
        )
        connection = await _load_connection(connection_id)
        start = datetime(2026, 3, 29, 14, 0, tzinfo=timezone.utc)

        async with AsyncSessionLocal() as session:
            acquired = await acquire_connection_lease(
                session=session,
                profile_id=profile_id,
                connection=connection,
                lease_kind="non_stream",
                lease_ttl_seconds=5,
                now_at=start,
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            summary = await reconcile_connection_runtime_state(
                session=session,
                profile_id=profile_id,
                connection_id=connection_id,
                now_at=start + timedelta(seconds=10),
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            remaining_state = (
                await session.execute(
                    select(RoutingConnectionRuntimeState).where(
                        RoutingConnectionRuntimeState.profile_id == profile_id,
                        RoutingConnectionRuntimeState.connection_id == connection_id,
                    )
                )
            ).scalar_one_or_none()
            remaining_leases = list(
                (
                    await session.execute(
                        select(RoutingConnectionRuntimeLease).where(
                            RoutingConnectionRuntimeLease.profile_id == profile_id,
                            RoutingConnectionRuntimeLease.connection_id
                            == connection_id,
                        )
                    )
                )
                .scalars()
                .all()
            )

        assert acquired.admitted is True
        assert summary["expired_leases_released"] == 1
        assert summary["state_rows_deleted"] == 1
        assert remaining_state is None
        assert remaining_leases == []
