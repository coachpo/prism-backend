from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import uuid4
from unittest.mock import ANY, AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.database import AsyncSessionLocal
from app.main import app
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
from app.services.monitoring.probe_runner import ProbeExecutionResult
from tests.loadbalance_strategy_helpers import make_routing_policy_adaptive


async def _seed_monitoring_route_fixture() -> dict[str, int]:
    suffix = uuid4().hex[:8]
    checked_at = datetime(2026, 3, 29, 14, 0, tzinfo=timezone.utc)

    async with AsyncSessionLocal() as session:
        profile = Profile(
            name=f"Monitoring Route Profile {suffix}",
            is_active=False,
            is_default=False,
            version=0,
        )
        other_profile = Profile(
            name=f"Monitoring Other Profile {suffix}",
            is_active=False,
            is_default=False,
            version=0,
        )
        openai_vendor = Vendor(
            key=f"openai-route-{suffix}",
            name=f"OpenAI Route {suffix}",
            audit_enabled=False,
            audit_capture_bodies=False,
        )
        anthropic_vendor = Vendor(
            key=f"anthropic-route-{suffix}",
            name=f"Anthropic Route {suffix}",
            audit_enabled=False,
            audit_capture_bodies=False,
        )
        hidden_vendor = Vendor(
            key=f"hidden-route-{suffix}",
            name=f"Hidden Route {suffix}",
            audit_enabled=False,
            audit_capture_bodies=False,
        )
        strategy = LoadbalanceStrategy(
            profile=profile,
            name=f"monitoring-route-strategy-{suffix}",
            routing_policy=make_routing_policy_adaptive(),
        )
        hidden_strategy = LoadbalanceStrategy(
            profile=other_profile,
            name=f"hidden-route-strategy-{suffix}",
            routing_policy=make_routing_policy_adaptive(),
        )
        openai_model = ModelConfig(
            profile=profile,
            vendor=openai_vendor,
            api_family="openai",
            model_id=f"gpt-route-{suffix}",
            display_name=f"GPT Route {suffix}",
            model_type="native",
            loadbalance_strategy=strategy,
            is_enabled=True,
        )
        anthropic_model = ModelConfig(
            profile=profile,
            vendor=anthropic_vendor,
            api_family="anthropic",
            model_id=f"claude-route-{suffix}",
            display_name=f"Claude Route {suffix}",
            model_type="native",
            loadbalance_strategy=strategy,
            is_enabled=True,
        )
        hidden_model = ModelConfig(
            profile=other_profile,
            vendor=hidden_vendor,
            api_family="openai",
            model_id=f"hidden-model-{suffix}",
            display_name=f"Hidden Model {suffix}",
            model_type="native",
            loadbalance_strategy=hidden_strategy,
            is_enabled=True,
        )
        endpoints = [
            Endpoint(
                profile=profile,
                name=f"route-endpoint-{suffix}-{index}",
                base_url=f"https://route-{suffix}-{index}.example.com/v1",
                api_key=f"sk-route-{suffix}-{index}",
                position=index,
            )
            for index in range(2)
        ]
        hidden_endpoint = Endpoint(
            profile=other_profile,
            name=f"hidden-endpoint-{suffix}",
            base_url=f"https://hidden-{suffix}.example.com/v1",
            api_key=f"sk-hidden-{suffix}",
            position=0,
        )
        openai_connection = Connection(
            profile=profile,
            model_config_rel=openai_model,
            endpoint_rel=endpoints[0],
            is_active=True,
            priority=0,
            name=f"openai-connection-{suffix}",
            monitoring_probe_interval_seconds=150,
        )
        anthropic_connection = Connection(
            profile=profile,
            model_config_rel=anthropic_model,
            endpoint_rel=endpoints[1],
            is_active=True,
            priority=0,
            name=f"anthropic-connection-{suffix}",
            monitoring_probe_interval_seconds=210,
        )
        hidden_connection = Connection(
            profile=other_profile,
            model_config_rel=hidden_model,
            endpoint_rel=hidden_endpoint,
            is_active=True,
            priority=0,
            name=f"hidden-connection-{suffix}",
        )
        session.add_all(
            [
                profile,
                other_profile,
                openai_vendor,
                anthropic_vendor,
                hidden_vendor,
                strategy,
                hidden_strategy,
                openai_model,
                anthropic_model,
                hidden_model,
                *endpoints,
                hidden_endpoint,
                openai_connection,
                anthropic_connection,
                hidden_connection,
            ]
        )
        await session.flush()

        session.add_all(
            [
                RoutingConnectionRuntimeState(
                    profile_id=profile.id,
                    connection_id=openai_connection.id,
                    circuit_state="closed",
                    last_probe_status="degraded",
                    last_probe_at=checked_at - timedelta(minutes=1),
                    endpoint_ping_ewma_ms=88.0,
                    conversation_delay_ewma_ms=190.0,
                ),
                RoutingConnectionRuntimeState(
                    profile_id=profile.id,
                    connection_id=anthropic_connection.id,
                    circuit_state="closed",
                    last_probe_status="healthy",
                    last_probe_at=checked_at - timedelta(minutes=2),
                    endpoint_ping_ewma_ms=70.0,
                    conversation_delay_ewma_ms=120.0,
                ),
                RoutingConnectionRuntimeState(
                    profile_id=other_profile.id,
                    connection_id=hidden_connection.id,
                    circuit_state="closed",
                    last_probe_status="healthy",
                    last_probe_at=checked_at - timedelta(minutes=3),
                    endpoint_ping_ewma_ms=40.0,
                    conversation_delay_ewma_ms=80.0,
                ),
            ]
        )
        session.add_all(
            [
                MonitoringConnectionProbeResult(
                    profile_id=profile.id,
                    vendor_id=openai_vendor.id,
                    model_config_id=openai_model.id,
                    connection_id=openai_connection.id,
                    endpoint_id=endpoints[0].id,
                    endpoint_ping_status="healthy",
                    endpoint_ping_ms=88,
                    conversation_status="unhealthy",
                    conversation_delay_ms=None,
                    failure_kind="timeout",
                    detail="conversation probe timed out",
                    checked_at=checked_at - timedelta(minutes=1),
                ),
                MonitoringConnectionProbeResult(
                    profile_id=profile.id,
                    vendor_id=openai_vendor.id,
                    model_config_id=openai_model.id,
                    connection_id=openai_connection.id,
                    endpoint_id=endpoints[0].id,
                    endpoint_ping_status="healthy",
                    endpoint_ping_ms=97,
                    conversation_status="healthy",
                    conversation_delay_ms=188,
                    failure_kind=None,
                    detail="previous healthy probe",
                    checked_at=checked_at - timedelta(minutes=5),
                ),
                MonitoringConnectionProbeResult(
                    profile_id=profile.id,
                    vendor_id=anthropic_vendor.id,
                    model_config_id=anthropic_model.id,
                    connection_id=anthropic_connection.id,
                    endpoint_id=endpoints[1].id,
                    endpoint_ping_status="healthy",
                    endpoint_ping_ms=70,
                    conversation_status="healthy",
                    conversation_delay_ms=120,
                    failure_kind=None,
                    detail="probe completed",
                    checked_at=checked_at - timedelta(minutes=2),
                ),
                MonitoringConnectionProbeResult(
                    profile_id=other_profile.id,
                    vendor_id=hidden_vendor.id,
                    model_config_id=hidden_model.id,
                    connection_id=hidden_connection.id,
                    endpoint_id=hidden_endpoint.id,
                    endpoint_ping_status="healthy",
                    endpoint_ping_ms=40,
                    conversation_status="healthy",
                    conversation_delay_ms=80,
                    failure_kind=None,
                    detail="hidden profile probe",
                    checked_at=checked_at - timedelta(minutes=3),
                ),
            ]
        )
        await session.commit()

        return {
            "profile_id": profile.id,
            "openai_vendor_id": openai_vendor.id,
            "openai_model_id": openai_model.id,
            "openai_connection_id": openai_connection.id,
            "openai_endpoint_id": endpoints[0].id,
            "anthropic_vendor_id": anthropic_vendor.id,
        }


class TestMonitoringManualProbeAndQueryRoutes:
    @pytest.mark.asyncio
    async def test_monitoring_overview_route_returns_profile_scoped_vendor_model_connection_tree(
        self,
    ):
        fixture = await _seed_monitoring_route_fixture()
        transport = ASGITransport(app=app)
        fixed_now = datetime(2026, 3, 29, 14, 0, tzinfo=timezone.utc)

        with patch("app.services.monitoring.queries.utc_now", return_value=fixed_now):
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                overview_response = await client.get(
                    "/api/monitoring/overview",
                    headers={"X-Profile-Id": str(fixture["profile_id"])},
                )

        assert overview_response.status_code == 200
        overview_payload = cast(dict[str, object], overview_response.json())
        vendors = cast(list[dict[str, object]], overview_payload["vendors"])
        vendor_keys = [vendor_item["vendor_key"] for vendor_item in vendors]
        assert len(vendor_keys) == 2
        assert all("hidden-route" not in str(vendor_key) for vendor_key in vendor_keys)

        openai_vendor = next(
            vendor
            for vendor in vendors
            if vendor["vendor_id"] == fixture["openai_vendor_id"]
        )
        assert openai_vendor["fused_status"] == "degraded"
        models = cast(list[dict[str, object]], openai_vendor["models"])
        assert len(models) == 1
        assert models[0]["model_config_id"] == fixture["openai_model_id"]
        connections = cast(list[dict[str, object]], models[0]["connections"])
        assert len(connections) == 1
        assert connections[0]["connection_id"] == fixture["openai_connection_id"]
        assert connections[0]["connection_name"] is not None
        assert connections[0]["monitoring_probe_interval_seconds"] == 150
        assert connections[0]["last_probe_status"] == "degraded"
        assert connections[0]["endpoint_ping_status"] == "healthy"
        assert connections[0]["conversation_status"] == "unhealthy"
        assert connections[0]["fused_status"] == "degraded"
        assert "availability_cells" not in connections[0]
        recent_history = cast(list[dict[str, object]], connections[0]["recent_history"])
        assert len(recent_history) == 2
        checked_at_values = [
            datetime.fromisoformat(cast(str, item["checked_at"]).replace("Z", "+00:00"))
            for item in recent_history
        ]
        assert checked_at_values == sorted(checked_at_values, reverse=True)
        assert [item["conversation_status"] for item in recent_history] == [
            "unhealthy",
            "healthy",
        ]
        assert recent_history[0]["endpoint_ping_status"] == "healthy"
        assert recent_history[0]["conversation_status"] == "unhealthy"

    @pytest.mark.asyncio
    async def test_monitoring_model_route_pins_model_detail_contract_fields_and_ordering(
        self,
    ):
        fixture = await _seed_monitoring_route_fixture()
        transport = ASGITransport(app=app)
        fixed_now = datetime(2026, 3, 29, 14, 0, tzinfo=timezone.utc)

        async with AsyncSessionLocal() as session:
            session.add_all(
                [
                    MonitoringConnectionProbeResult(
                        profile_id=fixture["profile_id"],
                        vendor_id=fixture["openai_vendor_id"],
                        model_config_id=fixture["openai_model_id"],
                        connection_id=fixture["openai_connection_id"],
                        endpoint_id=fixture["openai_endpoint_id"],
                        endpoint_ping_status="healthy",
                        endpoint_ping_ms=100 + minute_offset,
                        conversation_status="healthy",
                        conversation_delay_ms=200 + minute_offset,
                        failure_kind=None,
                        detail=f"older route probe {minute_offset}",
                        checked_at=fixed_now - timedelta(minutes=minute_offset),
                    )
                    for minute_offset in range(2, 62)
                    if minute_offset != 5
                ]
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            extra_endpoint = Endpoint(
                profile_id=fixture["profile_id"],
                name=f"route-endpoint-extra-{uuid4().hex[:6]}",
                base_url=f"https://route-extra-{uuid4().hex[:6]}.example.com/v1",
                api_key=f"sk-route-extra-{uuid4().hex[:6]}",
                position=9,
            )
            session.add(extra_endpoint)
            await session.flush()

            extra_connection = Connection(
                profile_id=fixture["profile_id"],
                model_config_id=fixture["openai_model_id"],
                endpoint_id=extra_endpoint.id,
                is_active=True,
                priority=1,
                name=f"openai-connection-secondary-{uuid4().hex[:6]}",
                monitoring_probe_interval_seconds=240,
            )
            session.add(extra_connection)
            await session.flush()

            session.add(
                RoutingConnectionRuntimeState(
                    profile_id=fixture["profile_id"],
                    connection_id=extra_connection.id,
                    circuit_state="closed",
                    last_probe_status="healthy",
                    last_probe_at=fixed_now - timedelta(minutes=3),
                    endpoint_ping_ewma_ms=91.0,
                    conversation_delay_ewma_ms=171.0,
                )
            )
            session.add_all(
                [
                    MonitoringConnectionProbeResult(
                        profile_id=fixture["profile_id"],
                        vendor_id=fixture["openai_vendor_id"],
                        model_config_id=fixture["openai_model_id"],
                        connection_id=extra_connection.id,
                        endpoint_id=extra_endpoint.id,
                        endpoint_ping_status="healthy",
                        endpoint_ping_ms=91,
                        conversation_status="healthy",
                        conversation_delay_ms=171,
                        failure_kind=None,
                        detail="secondary probe completed",
                        checked_at=fixed_now - timedelta(minutes=3),
                    ),
                    MonitoringConnectionProbeResult(
                        profile_id=fixture["profile_id"],
                        vendor_id=fixture["openai_vendor_id"],
                        model_config_id=fixture["openai_model_id"],
                        connection_id=extra_connection.id,
                        endpoint_id=extra_endpoint.id,
                        endpoint_ping_status="healthy",
                        endpoint_ping_ms=95,
                        conversation_status="healthy",
                        conversation_delay_ms=180,
                        failure_kind=None,
                        detail="older secondary probe",
                        checked_at=fixed_now - timedelta(minutes=8),
                    ),
                ]
            )
            await session.commit()
            extra_connection_id = extra_connection.id

        with patch("app.services.monitoring.queries.utc_now", return_value=fixed_now):
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                model_response = await client.get(
                    f"/api/monitoring/models/{fixture['openai_model_id']}",
                    headers={"X-Profile-Id": str(fixture["profile_id"])},
                )

        assert model_response.status_code == 200
        model_payload = cast(dict[str, object], model_response.json())
        assert model_payload["model_config_id"] == fixture["openai_model_id"]

        connections = cast(list[dict[str, object]], model_payload["connections"])
        assert [item["connection_id"] for item in connections] == [
            fixture["openai_connection_id"],
            extra_connection_id,
        ]

        required_model_detail_fields = {
            "last_probe_status",
            "last_probe_at",
            "endpoint_ping_status",
            "conversation_status",
            "fused_status",
            "recent_history",
        }
        for item in connections:
            assert required_model_detail_fields.issubset(item)
            assert "availability_cells" not in item
            recent_history = cast(list[dict[str, object]], item["recent_history"])
            checked_at_values = [
                datetime.fromisoformat(
                    cast(str, history_item["checked_at"]).replace("Z", "+00:00")
                )
                for history_item in recent_history
            ]
            assert checked_at_values == sorted(checked_at_values, reverse=True)

        primary_connection = connections[0]
        assert primary_connection["last_probe_status"] == "degraded"
        assert primary_connection["last_probe_at"] is not None
        assert primary_connection["endpoint_ping_status"] == "healthy"
        assert primary_connection["conversation_status"] == "unhealthy"
        assert primary_connection["fused_status"] == "degraded"
        primary_recent_history = cast(
            list[dict[str, object]], primary_connection["recent_history"]
        )
        assert len(primary_recent_history) == 60
        assert datetime.fromisoformat(
            cast(str, primary_recent_history[0]["checked_at"]).replace("Z", "+00:00")
        ) == fixed_now - timedelta(minutes=1)
        assert datetime.fromisoformat(
            cast(str, primary_recent_history[-1]["checked_at"]).replace("Z", "+00:00")
        ) == fixed_now - timedelta(minutes=60)
        assert primary_recent_history[0]["endpoint_ping_ms"] == 88
        assert primary_recent_history[-1]["endpoint_ping_ms"] == 160
        assert primary_recent_history[0]["conversation_status"] == "unhealthy"

        secondary_connection = connections[1]
        assert secondary_connection["last_probe_status"] == "healthy"
        assert secondary_connection["last_probe_at"] is not None
        assert secondary_connection["endpoint_ping_status"] == "healthy"
        assert secondary_connection["conversation_status"] == "healthy"
        assert secondary_connection["fused_status"] == "healthy"

    @pytest.mark.asyncio
    async def test_manual_probe_route_delegates_to_shared_probe_runner(self):
        fixture = await _seed_monitoring_route_fixture()
        transport = ASGITransport(app=app)
        probe_result = ProbeExecutionResult(
            connection_id=int(fixture["openai_connection_id"]),
            checked_at=datetime(2026, 3, 29, 15, 0, tzinfo=timezone.utc),
            endpoint_ping_status="healthy",
            endpoint_ping_ms=81,
            conversation_status="healthy",
            conversation_delay_ms=143,
            fused_status="healthy",
            failure_kind=None,
            detail="probe completed",
        )
        app.state.http_client = AsyncMock(name="shared-http-client")

        with patch(
            "app.routers.monitoring.run_connection_probe",
            new_callable=AsyncMock,
            return_value=probe_result,
        ) as probe_mock:
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                response = await client.post(
                    f"/api/monitoring/connections/{fixture['openai_connection_id']}/probe",
                    headers={"X-Profile-Id": str(fixture["profile_id"])},
                )

        assert response.status_code == 200
        payload = cast(dict[str, object], response.json())
        assert payload["connection_id"] == fixture["openai_connection_id"]
        assert payload["endpoint_ping_ms"] == 81
        assert payload["conversation_delay_ms"] == 143
        assert payload["fused_status"] == "healthy"
        probe_mock.assert_awaited_once_with(
            db=ANY,
            client=app.state.http_client,
            profile_id=fixture["profile_id"],
            connection_id=fixture["openai_connection_id"],
        )
