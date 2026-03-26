from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.models import (
    Connection,
    ConnectionLimiterLease,
    ConnectionLimiterState,
    Endpoint,
    ModelConfig,
    Profile,
    Vendor,
)
from tests.loadbalance_strategy_helpers import make_loadbalance_strategy


async def _create_connection_fixture(
    *,
    qps_limit: int | None = None,
    max_in_flight_non_stream: int | None = None,
    max_in_flight_stream: int | None = None,
) -> tuple[int, int]:
    suffix = uuid4().hex[:8]

    async with AsyncSessionLocal() as session:
        vendor = Vendor(
            key=f"openai-limiter-{suffix}",
            name=f"Limiter Vendor {suffix}",
            audit_enabled=False,
            audit_capture_bodies=False,
        )
        profile = Profile(name=f"Limiter Profile {suffix}", is_active=False, version=0)
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
            loadbalance_strategy=make_loadbalance_strategy(
                profile=profile,
                strategy_type="failover",
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


class TestLoadbalancerLimiter:
    @pytest.mark.asyncio
    async def test_acquire_connection_limit_enforces_qps_limit_within_window(
        self,
    ):
        from app.services.loadbalancer.limiter import acquire_connection_limit

        profile_id, connection_id = await _create_connection_fixture(qps_limit=2)
        connection = await _load_connection(connection_id)
        start = datetime(2026, 3, 26, 10, 0, tzinfo=timezone.utc)

        first = await acquire_connection_limit(
            profile_id=profile_id,
            connection=connection,
            lease_kind="non_stream",
            now_at=start,
            lease_ttl_seconds=30,
        )
        second = await acquire_connection_limit(
            profile_id=profile_id,
            connection=connection,
            lease_kind="non_stream",
            now_at=start + timedelta(milliseconds=200),
            lease_ttl_seconds=30,
        )
        third = await acquire_connection_limit(
            profile_id=profile_id,
            connection=connection,
            lease_kind="non_stream",
            now_at=start + timedelta(milliseconds=400),
            lease_ttl_seconds=30,
        )

        assert first.admitted is True
        assert first.lease_token is None
        assert second.admitted is True
        assert second.lease_token is None
        assert third.admitted is False
        assert third.deny_reason == "qps_limit"

    @pytest.mark.asyncio
    async def test_acquire_connection_limit_tracks_stream_and_non_stream_separately(
        self,
    ):
        from app.services.loadbalancer.limiter import acquire_connection_limit

        profile_id, connection_id = await _create_connection_fixture(
            max_in_flight_non_stream=1,
            max_in_flight_stream=1,
        )
        connection = await _load_connection(connection_id)
        start = datetime(2026, 3, 26, 11, 0, tzinfo=timezone.utc)

        non_stream = await acquire_connection_limit(
            profile_id=profile_id,
            connection=connection,
            lease_kind="non_stream",
            now_at=start,
            lease_ttl_seconds=30,
        )
        stream = await acquire_connection_limit(
            profile_id=profile_id,
            connection=connection,
            lease_kind="stream",
            now_at=start,
            lease_ttl_seconds=30,
        )
        blocked_non_stream = await acquire_connection_limit(
            profile_id=profile_id,
            connection=connection,
            lease_kind="non_stream",
            now_at=start,
            lease_ttl_seconds=30,
        )
        blocked_stream = await acquire_connection_limit(
            profile_id=profile_id,
            connection=connection,
            lease_kind="stream",
            now_at=start,
            lease_ttl_seconds=30,
        )

        assert non_stream.admitted is True
        assert non_stream.lease_token is not None
        assert stream.admitted is True
        assert stream.lease_token is not None
        assert blocked_non_stream.admitted is False
        assert blocked_non_stream.deny_reason == "in_flight_limit"
        assert blocked_stream.admitted is False
        assert blocked_stream.deny_reason == "in_flight_limit"

    @pytest.mark.asyncio
    async def test_release_connection_lease_reduces_in_flight_capacity_immediately(
        self,
    ):
        from app.services.loadbalancer.limiter import (
            acquire_connection_limit,
            release_connection_lease,
        )

        profile_id, connection_id = await _create_connection_fixture(
            max_in_flight_non_stream=1,
        )
        connection = await _load_connection(connection_id)
        start = datetime(2026, 3, 26, 12, 0, tzinfo=timezone.utc)

        acquired = await acquire_connection_limit(
            profile_id=profile_id,
            connection=connection,
            lease_kind="non_stream",
            now_at=start,
            lease_ttl_seconds=30,
        )
        blocked = await acquire_connection_limit(
            profile_id=profile_id,
            connection=connection,
            lease_kind="non_stream",
            now_at=start,
            lease_ttl_seconds=30,
        )
        released = await release_connection_lease(
            profile_id=profile_id,
            lease_token=acquired.lease_token,
        )
        reacquired = await acquire_connection_limit(
            profile_id=profile_id,
            connection=connection,
            lease_kind="non_stream",
            now_at=start + timedelta(seconds=1),
            lease_ttl_seconds=30,
        )

        assert acquired.admitted is True
        assert acquired.lease_token is not None
        assert blocked.admitted is False
        assert released is True
        assert reacquired.admitted is True

    @pytest.mark.asyncio
    async def test_reconcile_all_connection_limits_repairs_expired_leases_and_compacts_empty_rows(
        self,
    ):
        from app.services.loadbalancer.limiter import (
            acquire_connection_limit,
            reconcile_all_connection_limits,
        )

        profile_id, connection_id = await _create_connection_fixture(
            max_in_flight_non_stream=1,
        )
        connection = await _load_connection(connection_id)
        start = datetime(2026, 3, 26, 13, 0, tzinfo=timezone.utc)

        acquired = await acquire_connection_limit(
            profile_id=profile_id,
            connection=connection,
            lease_kind="non_stream",
            now_at=start,
            lease_ttl_seconds=5,
        )
        blocked = await acquire_connection_limit(
            profile_id=profile_id,
            connection=connection,
            lease_kind="non_stream",
            now_at=start + timedelta(seconds=1),
            lease_ttl_seconds=5,
        )
        summary = await reconcile_all_connection_limits(
            profile_id=profile_id,
            now_at=start + timedelta(seconds=10),
        )
        reacquired = await acquire_connection_limit(
            profile_id=profile_id,
            connection=connection,
            lease_kind="non_stream",
            now_at=start + timedelta(seconds=10),
            lease_ttl_seconds=5,
        )

        assert acquired.admitted is True
        assert acquired.lease_token is not None
        assert blocked.admitted is False
        assert summary["expired_leases_released"] >= 1
        assert reacquired.admitted is True

        async with AsyncSessionLocal() as session:
            remaining_leases = list(
                (
                    await session.execute(
                        select(ConnectionLimiterLease).where(
                            ConnectionLimiterLease.profile_id == profile_id,
                            ConnectionLimiterLease.connection_id == connection_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            state_row = (
                await session.execute(
                    select(ConnectionLimiterState).where(
                        ConnectionLimiterState.profile_id == profile_id,
                        ConnectionLimiterState.connection_id == connection_id,
                    )
                )
            ).scalar_one_or_none()

        assert len(remaining_leases) == 1
        assert state_row is not None

    @pytest.mark.asyncio
    async def test_lifespan_runs_limiter_reconciliation_after_startup_sequence(self):
        from app.main import app, lifespan

        mock_http_client = SimpleNamespace(aclose=AsyncMock())
        events: list[str] = []

        async def startup_sequence() -> None:
            events.append("startup")

        async def reconcile() -> dict[str, int]:
            events.append("reconcile")
            return {
                "expired_leases_released": 0,
                "state_rows_deleted": 0,
                "state_rows_updated": 0,
            }

        def build_http_client() -> SimpleNamespace:
            events.append("http_client")
            return mock_http_client

        with (
            patch(
                "app.main.bootstrap.run_startup_sequence",
                AsyncMock(side_effect=startup_sequence),
            ),
            patch(
                "app.main.reconcile_all_connection_limits",
                AsyncMock(side_effect=reconcile),
            ),
            patch(
                "app.main.bootstrap.build_http_client", side_effect=build_http_client
            ),
            patch("app.main.background_task_manager.start", AsyncMock()),
            patch("app.main.background_task_manager.shutdown", AsyncMock()),
            patch(
                "app.main.shutdown_dashboard_update_lifecycle",
                AsyncMock(),
            ),
            patch(
                "app.main.get_engine",
                return_value=SimpleNamespace(dispose=AsyncMock()),
            ),
        ):
            async with lifespan(app):
                pass

        assert events[:3] == ["startup", "reconcile", "http_client"]
