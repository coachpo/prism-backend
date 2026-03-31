from __future__ import annotations

import importlib
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
    MonitoringConnectionProbeResult,
    Profile,
    RoutingConnectionRuntimeState,
    Vendor,
)
from app.services.loadbalancer.runtime_store import record_connection_failure_state
from tests.loadbalance_strategy_helpers import make_routing_policy_adaptive


def _load_module(module_name: str):
    try:
        return importlib.import_module(module_name)
    except Exception as exc:
        pytest.fail(f"{module_name} must exist for monitoring routing feedback: {exc}")


async def _seed_feedback_fixture() -> dict[str, int]:
    suffix = uuid4().hex[:8]

    async with AsyncSessionLocal() as session:
        vendor = Vendor(
            key=f"monitoring-feedback-{suffix}",
            name=f"Monitoring Feedback {suffix}",
            audit_enabled=False,
            audit_capture_bodies=False,
        )
        profile = Profile(
            name=f"Monitoring Feedback Profile {suffix}",
            is_active=False,
            version=0,
        )
        endpoint = Endpoint(
            profile=profile,
            name=f"feedback-endpoint-{suffix}",
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            position=0,
        )
        strategy = LoadbalanceStrategy(
            profile=profile,
            name=f"feedback-strategy-{suffix}",
            routing_policy=make_routing_policy_adaptive(),
        )
        model = ModelConfig(
            vendor=vendor,
            profile=profile,
            api_family="openai",
            model_id=f"model-{suffix}",
            model_type="native",
            loadbalance_strategy=strategy,
            is_enabled=True,
        )
        connection = Connection(
            profile=profile,
            model_config_rel=model,
            endpoint_rel=endpoint,
            is_active=True,
            priority=0,
            name=f"feedback-connection-{suffix}",
        )
        session.add_all([vendor, profile, endpoint, strategy, model, connection])
        await session.commit()
        return {
            "profile_id": profile.id,
            "vendor_id": vendor.id,
            "model_config_id": model.id,
            "connection_id": connection.id,
            "endpoint_id": endpoint.id,
        }


class TestMonitoringRoutingFeedback:
    @pytest.mark.asyncio
    async def test_record_probe_outcome_persists_history_and_opens_runtime_state_on_failure(
        self,
    ):
        feedback_module = _load_module("app.services.monitoring.routing_feedback")
        record_probe_outcome = getattr(feedback_module, "record_probe_outcome", None)
        assert record_probe_outcome is not None, (
            "app.services.monitoring.routing_feedback.record_probe_outcome must exist"
        )

        fixture = await _seed_feedback_fixture()
        checked_at = datetime(2026, 3, 29, 17, 0, tzinfo=timezone.utc)

        await record_probe_outcome(
            profile_id=fixture["profile_id"],
            vendor_id=fixture["vendor_id"],
            model_config_id=fixture["model_config_id"],
            connection_id=fixture["connection_id"],
            endpoint_id=fixture["endpoint_id"],
            endpoint_ping_status="unhealthy",
            endpoint_ping_ms=None,
            conversation_status="unhealthy",
            conversation_delay_ms=None,
            failure_kind="timeout",
            detail="Connection timed out",
            checked_at=checked_at,
        )

        async with AsyncSessionLocal() as session:
            history_count = await session.scalar(
                select(func.count())
                .select_from(MonitoringConnectionProbeResult)
                .where(
                    MonitoringConnectionProbeResult.profile_id == fixture["profile_id"],
                    MonitoringConnectionProbeResult.connection_id
                    == fixture["connection_id"],
                )
            )
            state_row = (
                await session.execute(
                    select(RoutingConnectionRuntimeState).where(
                        RoutingConnectionRuntimeState.profile_id
                        == fixture["profile_id"],
                        RoutingConnectionRuntimeState.connection_id
                        == fixture["connection_id"],
                    )
                )
            ).scalar_one()

        assert history_count == 1
        assert state_row.last_probe_status == "unhealthy"
        assert state_row.circuit_state == "open"
        assert state_row.last_probe_at == checked_at
        assert state_row.last_live_failure_at is None
        assert state_row.last_live_success_at is None

    @pytest.mark.asyncio
    async def test_record_probe_outcome_recovers_open_circuit_only_after_successful_probe_hysteresis(
        self,
    ):
        feedback_module = _load_module("app.services.monitoring.routing_feedback")
        record_probe_outcome = getattr(feedback_module, "record_probe_outcome", None)
        assert record_probe_outcome is not None, (
            "app.services.monitoring.routing_feedback.record_probe_outcome must exist"
        )

        fixture = await _seed_feedback_fixture()
        opened_at = datetime(2026, 3, 29, 18, 0, tzinfo=timezone.utc)
        recovered_at = opened_at + timedelta(seconds=61)

        async with AsyncSessionLocal() as session:
            _ = await record_connection_failure_state(
                session=session,
                profile_id=fixture["profile_id"],
                connection_id=fixture["connection_id"],
                failure_kind="timeout",
                cooldown_seconds=60.0,
                strike_incremented=False,
                ban_mode="off",
                ban_duration_seconds=0,
                now_at=opened_at,
            )
            await session.commit()

        await record_probe_outcome(
            profile_id=fixture["profile_id"],
            vendor_id=fixture["vendor_id"],
            model_config_id=fixture["model_config_id"],
            connection_id=fixture["connection_id"],
            endpoint_id=fixture["endpoint_id"],
            endpoint_ping_status="healthy",
            endpoint_ping_ms=80,
            conversation_status="healthy",
            conversation_delay_ms=140,
            failure_kind=None,
            detail="probe completed",
            checked_at=recovered_at,
        )

        async with AsyncSessionLocal() as session:
            state_row = (
                await session.execute(
                    select(RoutingConnectionRuntimeState).where(
                        RoutingConnectionRuntimeState.profile_id
                        == fixture["profile_id"],
                        RoutingConnectionRuntimeState.connection_id
                        == fixture["connection_id"],
                    )
                )
            ).scalar_one()

        assert state_row.circuit_state == "closed"
        assert state_row.consecutive_failures == 0
        assert state_row.last_probe_status == "healthy"

    @pytest.mark.asyncio
    async def test_record_passive_request_outcome_updates_runtime_fields_without_creating_probe_history(
        self,
    ):
        feedback_module = _load_module("app.services.monitoring.routing_feedback")
        record_passive_request_outcome = getattr(
            feedback_module,
            "record_passive_request_outcome",
            None,
        )
        assert record_passive_request_outcome is not None, (
            "app.services.monitoring.routing_feedback.record_passive_request_outcome must exist"
        )

        fixture = await _seed_feedback_fixture()
        observed_at = datetime(2026, 3, 29, 19, 0, tzinfo=timezone.utc)

        await record_passive_request_outcome(
            profile_id=fixture["profile_id"],
            connection_id=fixture["connection_id"],
            status_code=503,
            response_time_ms=245,
            success_flag=False,
            observed_at=observed_at,
        )

        async with AsyncSessionLocal() as session:
            history_count = await session.scalar(
                select(func.count())
                .select_from(MonitoringConnectionProbeResult)
                .where(
                    MonitoringConnectionProbeResult.profile_id == fixture["profile_id"],
                    MonitoringConnectionProbeResult.connection_id
                    == fixture["connection_id"],
                )
            )
            state_row = (
                await session.execute(
                    select(RoutingConnectionRuntimeState).where(
                        RoutingConnectionRuntimeState.profile_id
                        == fixture["profile_id"],
                        RoutingConnectionRuntimeState.connection_id
                        == fixture["connection_id"],
                    )
                )
            ).scalar_one()

        assert history_count == 0
        assert state_row.last_probe_status is None
        assert state_row.last_live_failure_kind == "transient_http"
        assert state_row.last_live_failure_at == observed_at
        assert state_row.live_p95_latency_ms is not None
        assert float(state_row.live_p95_latency_ms) == pytest.approx(245.0)

    @pytest.mark.asyncio
    async def test_successful_probe_does_not_clear_recent_passive_live_failure_evidence(
        self,
    ):
        feedback_module = _load_module("app.services.monitoring.routing_feedback")
        record_passive_request_outcome = getattr(
            feedback_module,
            "record_passive_request_outcome",
            None,
        )
        record_probe_outcome = getattr(feedback_module, "record_probe_outcome", None)
        assert record_passive_request_outcome is not None, (
            "app.services.monitoring.routing_feedback.record_passive_request_outcome must exist"
        )
        assert record_probe_outcome is not None, (
            "app.services.monitoring.routing_feedback.record_probe_outcome must exist"
        )

        fixture = await _seed_feedback_fixture()
        failure_at = datetime(2026, 3, 29, 20, 0, tzinfo=timezone.utc)
        probe_at = failure_at + timedelta(seconds=30)

        await record_passive_request_outcome(
            profile_id=fixture["profile_id"],
            connection_id=fixture["connection_id"],
            status_code=503,
            response_time_ms=300,
            success_flag=False,
            observed_at=failure_at,
        )
        await record_probe_outcome(
            profile_id=fixture["profile_id"],
            vendor_id=fixture["vendor_id"],
            model_config_id=fixture["model_config_id"],
            connection_id=fixture["connection_id"],
            endpoint_id=fixture["endpoint_id"],
            endpoint_ping_status="healthy",
            endpoint_ping_ms=80,
            conversation_status="healthy",
            conversation_delay_ms=140,
            failure_kind=None,
            detail="probe completed",
            checked_at=probe_at,
        )

        async with AsyncSessionLocal() as session:
            state_row = (
                await session.execute(
                    select(RoutingConnectionRuntimeState).where(
                        RoutingConnectionRuntimeState.profile_id
                        == fixture["profile_id"],
                        RoutingConnectionRuntimeState.connection_id
                        == fixture["connection_id"],
                    )
                )
            ).scalar_one()

        assert state_row.last_probe_status == "healthy"
        assert state_row.last_probe_at == probe_at
        assert state_row.last_live_failure_kind == "transient_http"
        assert state_row.last_live_failure_at == failure_at
        assert state_row.last_live_success_at is None
