import importlib
from datetime import datetime, timedelta, timezone
from typing import TypedDict
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

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
from tests.loadbalance_strategy_helpers import make_routing_policy_adaptive


class MonitoringQueryFixture(TypedDict):
    profile_id: int
    openai_vendor_id: int
    openai_vendor_key: str
    openai_primary_model_id: int
    openai_primary_model_key: str
    openai_backup_model_id: int
    anthropic_vendor_id: int
    anthropic_vendor_key: str
    anthropic_model_id: int
    primary_healthy_connection_id: int
    primary_degraded_connection_id: int
    primary_degraded_endpoint_id: int
    backup_unhealthy_connection_id: int


def _load_module(module_name: str):
    try:
        return importlib.import_module(module_name)
    except Exception as exc:
        pytest.fail(f"{module_name} must exist for monitoring queries: {exc}")


def _require_attr(module: object, attr_name: str):
    value = getattr(module, attr_name, None)
    module_name = getattr(module, "__name__", type(module).__name__)
    assert value is not None, f"{module_name}.{attr_name} must exist"
    return value


async def _seed_monitoring_query_fixture() -> MonitoringQueryFixture:
    suffix = uuid4().hex[:8]
    checked_at = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)

    async with AsyncSessionLocal() as session:
        fixture = await _insert_monitoring_query_fixture(
            session=session,
            suffix=suffix,
            checked_at=checked_at,
        )
        await session.commit()
        return fixture


async def _insert_monitoring_query_fixture(
    *,
    session: AsyncSession,
    suffix: str,
    checked_at: datetime,
) -> MonitoringQueryFixture:
    profile = Profile(
        name=f"Monitoring Query Profile {suffix}",
        is_active=False,
        is_default=False,
        version=0,
    )
    openai_vendor = Vendor(
        key=f"openai-monitoring-{suffix}",
        name=f"OpenAI Monitoring {suffix}",
        icon_key="openai",
        audit_enabled=False,
        audit_capture_bodies=False,
    )
    anthropic_vendor = Vendor(
        key=f"anthropic-monitoring-{suffix}",
        name=f"Anthropic Monitoring {suffix}",
        icon_key="anthropic",
        audit_enabled=False,
        audit_capture_bodies=False,
    )
    strategy = LoadbalanceStrategy(
        profile=profile,
        name=f"monitoring-query-strategy-{suffix}",
        routing_policy=make_routing_policy_adaptive(),
    )
    openai_primary_model = ModelConfig(
        profile=profile,
        vendor=openai_vendor,
        api_family="openai",
        model_id=f"gpt-primary-{suffix}",
        display_name=f"GPT Primary {suffix}",
        model_type="native",
        loadbalance_strategy=strategy,
        is_enabled=True,
    )
    openai_backup_model = ModelConfig(
        profile=profile,
        vendor=openai_vendor,
        api_family="openai",
        model_id=f"gpt-backup-{suffix}",
        display_name=f"GPT Backup {suffix}",
        model_type="native",
        loadbalance_strategy=strategy,
        is_enabled=True,
    )
    anthropic_model = ModelConfig(
        profile=profile,
        vendor=anthropic_vendor,
        api_family="anthropic",
        model_id=f"claude-{suffix}",
        display_name=f"Claude {suffix}",
        model_type="native",
        loadbalance_strategy=strategy,
        is_enabled=True,
    )
    disabled_model = ModelConfig(
        profile=profile,
        vendor=openai_vendor,
        api_family="openai",
        model_id=f"disabled-{suffix}",
        display_name=f"Disabled {suffix}",
        model_type="native",
        loadbalance_strategy=strategy,
        is_enabled=False,
    )
    endpoints = [
        Endpoint(
            profile=profile,
            name=f"endpoint-{suffix}-{index}",
            base_url=f"https://monitoring-{suffix}-{index}.example.com/v1",
            api_key=f"sk-monitoring-{suffix}-{index}",
            position=index,
        )
        for index in range(5)
    ]
    primary_healthy_connection = Connection(
        profile=profile,
        model_config_rel=openai_primary_model,
        endpoint_rel=endpoints[0],
        is_active=True,
        priority=0,
        name=f"primary-healthy-{suffix}",
        monitoring_probe_interval_seconds=180,
    )
    primary_degraded_connection = Connection(
        profile=profile,
        model_config_rel=openai_primary_model,
        endpoint_rel=endpoints[1],
        is_active=True,
        priority=1,
        name=f"primary-degraded-{suffix}",
        monitoring_probe_interval_seconds=240,
    )
    primary_inactive_connection = Connection(
        profile=profile,
        model_config_rel=openai_primary_model,
        endpoint_rel=endpoints[2],
        is_active=False,
        priority=2,
        name=f"primary-inactive-{suffix}",
    )
    backup_unhealthy_connection = Connection(
        profile=profile,
        model_config_rel=openai_backup_model,
        endpoint_rel=endpoints[3],
        is_active=True,
        priority=0,
        name=f"backup-unhealthy-{suffix}",
    )
    anthropic_healthy_connection = Connection(
        profile=profile,
        model_config_rel=anthropic_model,
        endpoint_rel=endpoints[4],
        is_active=True,
        priority=0,
        name=f"anthropic-healthy-{suffix}",
    )
    disabled_connection = Connection(
        profile=profile,
        model_config_rel=disabled_model,
        endpoint_rel=endpoints[2],
        is_active=True,
        priority=0,
        name=f"disabled-{suffix}",
    )

    session.add_all(
        [
            profile,
            openai_vendor,
            anthropic_vendor,
            strategy,
            openai_primary_model,
            openai_backup_model,
            anthropic_model,
            disabled_model,
            *endpoints,
            primary_healthy_connection,
            primary_degraded_connection,
            primary_inactive_connection,
            backup_unhealthy_connection,
            anthropic_healthy_connection,
            disabled_connection,
        ]
    )
    await session.flush()

    session.add_all(
        [
            RoutingConnectionRuntimeState(
                profile_id=profile.id,
                connection_id=primary_healthy_connection.id,
                circuit_state="closed",
                last_probe_status="healthy",
                last_probe_at=checked_at - timedelta(minutes=1),
                endpoint_ping_ewma_ms=82.0,
                conversation_delay_ewma_ms=145.0,
            ),
            RoutingConnectionRuntimeState(
                profile_id=profile.id,
                connection_id=primary_degraded_connection.id,
                circuit_state="closed",
                last_probe_status="degraded",
                last_probe_at=checked_at - timedelta(minutes=2),
                endpoint_ping_ewma_ms=115.0,
                conversation_delay_ewma_ms=310.0,
            ),
            RoutingConnectionRuntimeState(
                profile_id=profile.id,
                connection_id=backup_unhealthy_connection.id,
                circuit_state="open",
                blocked_until_at=checked_at + timedelta(minutes=5),
                probe_available_at=checked_at + timedelta(minutes=5),
                last_probe_status="unhealthy",
                last_probe_at=checked_at - timedelta(minutes=3),
                endpoint_ping_ewma_ms=400.0,
                conversation_delay_ewma_ms=800.0,
            ),
            RoutingConnectionRuntimeState(
                profile_id=profile.id,
                connection_id=anthropic_healthy_connection.id,
                circuit_state="closed",
                last_probe_status="healthy",
                last_probe_at=checked_at - timedelta(minutes=4),
                endpoint_ping_ewma_ms=95.0,
                conversation_delay_ewma_ms=165.0,
            ),
        ]
    )
    session.add_all(
        [
            MonitoringConnectionProbeResult(
                profile_id=profile.id,
                vendor_id=openai_vendor.id,
                model_config_id=openai_primary_model.id,
                connection_id=primary_healthy_connection.id,
                endpoint_id=endpoints[0].id,
                endpoint_ping_status="healthy",
                endpoint_ping_ms=82,
                conversation_status="healthy",
                conversation_delay_ms=145,
                failure_kind=None,
                detail="probe completed",
                checked_at=checked_at - timedelta(minutes=1),
            ),
            MonitoringConnectionProbeResult(
                profile_id=profile.id,
                vendor_id=openai_vendor.id,
                model_config_id=openai_primary_model.id,
                connection_id=primary_healthy_connection.id,
                endpoint_id=endpoints[0].id,
                endpoint_ping_status="healthy",
                endpoint_ping_ms=84,
                conversation_status="degraded",
                conversation_delay_ms=210,
                failure_kind="timeout",
                detail="previous probe degraded",
                checked_at=checked_at - timedelta(minutes=6),
            ),
            MonitoringConnectionProbeResult(
                profile_id=profile.id,
                vendor_id=openai_vendor.id,
                model_config_id=openai_primary_model.id,
                connection_id=primary_degraded_connection.id,
                endpoint_id=endpoints[1].id,
                endpoint_ping_status="healthy",
                endpoint_ping_ms=115,
                conversation_status="unhealthy",
                conversation_delay_ms=None,
                failure_kind="timeout",
                detail="conversation probe timed out",
                checked_at=checked_at - timedelta(minutes=2),
            ),
            MonitoringConnectionProbeResult(
                profile_id=profile.id,
                vendor_id=openai_vendor.id,
                model_config_id=openai_backup_model.id,
                connection_id=backup_unhealthy_connection.id,
                endpoint_id=endpoints[3].id,
                endpoint_ping_status="unhealthy",
                endpoint_ping_ms=None,
                conversation_status="unhealthy",
                conversation_delay_ms=None,
                failure_kind="connect_error",
                detail="endpoint connection failed",
                checked_at=checked_at - timedelta(minutes=3),
            ),
            MonitoringConnectionProbeResult(
                profile_id=profile.id,
                vendor_id=anthropic_vendor.id,
                model_config_id=anthropic_model.id,
                connection_id=anthropic_healthy_connection.id,
                endpoint_id=endpoints[4].id,
                endpoint_ping_status="healthy",
                endpoint_ping_ms=95,
                conversation_status="healthy",
                conversation_delay_ms=165,
                failure_kind=None,
                detail="probe completed",
                checked_at=checked_at - timedelta(minutes=4),
            ),
            MonitoringConnectionProbeResult(
                profile_id=profile.id,
                vendor_id=openai_vendor.id,
                model_config_id=openai_primary_model.id,
                connection_id=primary_inactive_connection.id,
                endpoint_id=endpoints[2].id,
                endpoint_ping_status="healthy",
                endpoint_ping_ms=50,
                conversation_status="healthy",
                conversation_delay_ms=90,
                failure_kind=None,
                detail="inactive connection should be ignored",
                checked_at=checked_at - timedelta(minutes=5),
            ),
            MonitoringConnectionProbeResult(
                profile_id=profile.id,
                vendor_id=openai_vendor.id,
                model_config_id=disabled_model.id,
                connection_id=disabled_connection.id,
                endpoint_id=endpoints[2].id,
                endpoint_ping_status="healthy",
                endpoint_ping_ms=65,
                conversation_status="healthy",
                conversation_delay_ms=105,
                failure_kind=None,
                detail="disabled model should be ignored",
                checked_at=checked_at - timedelta(minutes=7),
            ),
        ]
    )

    return {
        "profile_id": profile.id,
        "openai_vendor_id": openai_vendor.id,
        "openai_vendor_key": openai_vendor.key,
        "openai_primary_model_id": openai_primary_model.id,
        "openai_primary_model_key": openai_primary_model.model_id,
        "openai_backup_model_id": openai_backup_model.id,
        "anthropic_vendor_id": anthropic_vendor.id,
        "anthropic_vendor_key": anthropic_vendor.key,
        "anthropic_model_id": anthropic_model.id,
        "primary_healthy_connection_id": primary_healthy_connection.id,
        "primary_degraded_connection_id": primary_degraded_connection.id,
        "primary_degraded_endpoint_id": endpoints[1].id,
        "backup_unhealthy_connection_id": backup_unhealthy_connection.id,
    }


class TestMonitoringQueryContracts:
    def test_monitoring_router_mounts_overview_vendor_model_and_probe_routes(self):
        monitoring_module = _load_module("app.routers.monitoring")

        registered_routes = {
            (route.path, method)
            for route in monitoring_module.router.routes
            if isinstance(route, APIRoute)
            for method in route.methods or set()
        }

        assert ("/api/monitoring/overview", "GET") in registered_routes
        assert ("/api/monitoring/vendors/{vendor_id}", "GET") in registered_routes
        assert (
            "/api/monitoring/models/{model_config_id}",
            "GET",
        ) in registered_routes
        assert (
            "/api/monitoring/connections/{connection_id}/probe",
            "POST",
        ) in registered_routes

    def test_monitoring_overview_schema_returns_vendor_model_connection_tree(self):
        schemas = _load_module("app.schemas.schemas")
        MonitoringOverviewResponse = _require_attr(
            schemas, "MonitoringOverviewResponse"
        )

        payload = MonitoringOverviewResponse.model_validate(
            {
                "generated_at": datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc),
                "vendors": [
                    {
                        "vendor_id": 11,
                        "vendor_key": "openai",
                        "vendor_name": "OpenAI",
                        "icon_key": "openai",
                        "fused_status": "degraded",
                        "model_count": 2,
                        "connection_count": 3,
                        "healthy_connection_count": 2,
                        "degraded_connection_count": 1,
                        "models": [
                            {
                                "model_config_id": 21,
                                "model_id": "gpt-4.1-mini",
                                "display_name": "GPT 4.1 Mini",
                                "fused_status": "degraded",
                                "connection_count": 2,
                                "connections": [
                                    {
                                        "connection_id": 31,
                                        "connection_name": "primary-openai",
                                        "endpoint_id": 41,
                                        "endpoint_name": "primary-endpoint",
                                        "monitoring_probe_interval_seconds": 180,
                                        "last_probe_status": "healthy",
                                        "last_probe_at": datetime(
                                            2026, 3, 29, 11, 59, tzinfo=timezone.utc
                                        ),
                                        "circuit_state": "closed",
                                        "live_p95_latency_ms": 123,
                                        "last_live_failure_kind": None,
                                        "last_live_failure_at": None,
                                        "last_live_success_at": datetime(
                                            2026, 3, 29, 11, 58, tzinfo=timezone.utc
                                        ),
                                        "endpoint_ping_status": "healthy",
                                        "endpoint_ping_ms": 82,
                                        "conversation_status": "healthy",
                                        "conversation_delay_ms": 145,
                                        "fused_status": "healthy",
                                        "recent_history": [
                                            {
                                                "checked_at": datetime(
                                                    2026,
                                                    3,
                                                    29,
                                                    11,
                                                    59,
                                                    tzinfo=timezone.utc,
                                                ),
                                                "endpoint_ping_status": "healthy",
                                                "endpoint_ping_ms": 82,
                                                "conversation_status": "healthy",
                                                "conversation_delay_ms": 145,
                                                "failure_kind": None,
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        )

        assert len(payload.vendors) == 1
        assert payload.vendors[0].vendor_id == 11
        assert payload.vendors[0].icon_key == "openai"
        assert payload.vendors[0].model_count == 2
        assert payload.vendors[0].models[0].model_config_id == 21
        assert payload.vendors[0].models[0].connections[0].connection_id == 31
        assert payload.vendors[0].models[0].connections[0].live_p95_latency_ms == 123
        assert len(payload.vendors[0].models[0].connections[0].recent_history) == 1
        assert not hasattr(
            payload.vendors[0].models[0].connections[0], "availability_cells"
        )

    def test_monitoring_vendor_schema_groups_by_model(self):
        schemas = _load_module("app.schemas.schemas")
        MonitoringVendorResponse = _require_attr(schemas, "MonitoringVendorResponse")

        payload = MonitoringVendorResponse.model_validate(
            {
                "generated_at": datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc),
                "vendor_id": 11,
                "vendor_key": "openai",
                "vendor_name": "OpenAI",
                "models": [
                    {
                        "model_config_id": 21,
                        "model_id": "gpt-4.1-mini",
                        "display_name": "GPT 4.1 Mini",
                        "fused_status": "healthy",
                        "connection_count": 2,
                    }
                ],
            }
        )

        assert payload.vendor_id == 11
        assert len(payload.models) == 1
        assert payload.models[0].model_config_id == 21

    def test_monitoring_model_schema_returns_connection_rows_and_recent_history(self):
        schemas = _load_module("app.schemas.schemas")
        MonitoringModelResponse = _require_attr(schemas, "MonitoringModelResponse")

        payload = MonitoringModelResponse.model_validate(
            {
                "generated_at": datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc),
                "vendor_id": 11,
                "vendor_key": "openai",
                "vendor_name": "OpenAI",
                "model_config_id": 21,
                "model_id": "gpt-4.1-mini",
                "display_name": "GPT 4.1 Mini",
                "connections": [
                    {
                        "connection_id": 31,
                        "endpoint_id": 41,
                        "endpoint_name": "primary-openai",
                        "monitoring_probe_interval_seconds": 180,
                        "endpoint_ping_status": "healthy",
                        "endpoint_ping_ms": 82,
                        "conversation_status": "healthy",
                        "conversation_delay_ms": 145,
                        "fused_status": "healthy",
                        "recent_history": [
                            {
                                "checked_at": datetime(
                                    2026, 3, 29, 11, 59, tzinfo=timezone.utc
                                ),
                                "endpoint_ping_status": "healthy",
                                "endpoint_ping_ms": 82,
                                "conversation_status": "healthy",
                                "conversation_delay_ms": 145,
                                "failure_kind": None,
                            }
                        ],
                    }
                ],
            }
        )

        assert payload.model_config_id == 21
        assert len(payload.connections) == 1
        assert payload.connections[0].connection_id == 31
        assert payload.connections[0].monitoring_probe_interval_seconds == 180
        assert payload.connections[0].endpoint_ping_ms == 82
        assert payload.connections[0].conversation_delay_ms == 145
        assert payload.connections[0].fused_status == "healthy"
        assert len(payload.connections[0].recent_history) == 1

    def test_manual_probe_schema_is_connection_scoped(self):
        schemas = _load_module("app.schemas.schemas")
        MonitoringManualProbeResponse = _require_attr(
            schemas, "MonitoringManualProbeResponse"
        )

        payload = MonitoringManualProbeResponse.model_validate(
            {
                "connection_id": 31,
                "checked_at": datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc),
                "endpoint_ping_status": "healthy",
                "endpoint_ping_ms": 81,
                "conversation_status": "healthy",
                "conversation_delay_ms": 143,
                "fused_status": "healthy",
                "failure_kind": None,
                "detail": "probe completed",
            }
        )

        assert payload.connection_id == 31
        assert payload.endpoint_ping_status == "healthy"
        assert payload.conversation_status == "healthy"


class TestMonitoringQueryBehavior:
    @pytest.mark.asyncio
    async def test_query_monitoring_overview_groups_enabled_active_connections_by_vendor(
        self,
    ):
        queries_module = _load_module("app.services.monitoring.queries")
        query_monitoring_overview = _require_attr(
            queries_module,
            "query_monitoring_overview",
        )
        fixture = await _seed_monitoring_query_fixture()
        fixed_now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)

        async with AsyncSessionLocal() as session:
            session.add_all(
                [
                    MonitoringConnectionProbeResult(
                        profile_id=fixture["profile_id"],
                        vendor_id=fixture["openai_vendor_id"],
                        model_config_id=fixture["openai_primary_model_id"],
                        connection_id=fixture["primary_degraded_connection_id"],
                        endpoint_id=fixture["primary_degraded_endpoint_id"],
                        endpoint_ping_status="healthy",
                        endpoint_ping_ms=100 + minute_offset,
                        conversation_status="healthy",
                        conversation_delay_ms=200 + minute_offset,
                        failure_kind=None,
                        detail=f"older probe {minute_offset}",
                        checked_at=fixed_now - timedelta(minutes=minute_offset),
                    )
                    for minute_offset in range(3, 63)
                ]
            )
            await session.commit()

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(queries_module, "utc_now", lambda: fixed_now)
            async with AsyncSessionLocal() as session:
                response = await query_monitoring_overview(
                    db=session,
                    profile_id=fixture["profile_id"],
                )

        vendors_by_key = {item.vendor_key: item for item in response.vendors}
        assert sorted(vendors_by_key) == [
            str(fixture["anthropic_vendor_key"]),
            str(fixture["openai_vendor_key"]),
        ]

        openai_item = vendors_by_key[str(fixture["openai_vendor_key"])]
        assert openai_item.icon_key == "openai"
        assert openai_item.fused_status == "degraded"
        assert openai_item.model_count == 2
        assert openai_item.connection_count == 3
        assert openai_item.healthy_connection_count == 1
        assert openai_item.degraded_connection_count == 2
        assert [model.model_config_id for model in openai_item.models] == [
            fixture["openai_backup_model_id"],
            fixture["openai_primary_model_id"],
        ]
        primary_model = openai_item.models[1]
        assert [row.connection_id for row in primary_model.connections] == [
            fixture["primary_healthy_connection_id"],
            fixture["primary_degraded_connection_id"],
        ]
        healthy_row = primary_model.connections[0]
        assert healthy_row.connection_name is not None
        assert healthy_row.last_probe_status == "healthy"
        assert healthy_row.circuit_state == "closed"
        assert healthy_row.live_p95_latency_ms is None
        assert not hasattr(healthy_row, "availability_cells")

        degraded_row = primary_model.connections[1]
        assert len(degraded_row.recent_history) == 60
        assert degraded_row.recent_history[0].checked_at == fixed_now - timedelta(
            minutes=2
        )
        assert degraded_row.recent_history[-1].checked_at == fixed_now - timedelta(
            minutes=61
        )
        assert degraded_row.recent_history[0].endpoint_ping_ms == 115
        assert degraded_row.recent_history[-1].endpoint_ping_ms == 161
        assert degraded_row.recent_history[0].conversation_status == "unhealthy"
        assert degraded_row.recent_history[0].conversation_delay_ms is None
        assert degraded_row.recent_history[0].failure_kind == "timeout"
        assert degraded_row.last_probe_at == fixed_now - timedelta(minutes=2)
        assert degraded_row.endpoint_ping_ms == 115
        assert degraded_row.conversation_status == "unhealthy"
        assert degraded_row.conversation_delay_ms is None

        anthropic_item = vendors_by_key[str(fixture["anthropic_vendor_key"])]
        assert anthropic_item.icon_key == "anthropic"
        assert anthropic_item.fused_status == "healthy"
        assert anthropic_item.model_count == 1
        assert anthropic_item.connection_count == 1
        assert anthropic_item.healthy_connection_count == 1
        assert anthropic_item.degraded_connection_count == 0
        assert len(anthropic_item.models) == 1

    @pytest.mark.asyncio
    async def test_query_monitoring_vendor_groups_connections_by_model_and_rolls_up_status(
        self,
    ):
        queries_module = _load_module("app.services.monitoring.queries")
        query_monitoring_vendor = _require_attr(
            queries_module,
            "query_monitoring_vendor",
        )
        fixture = await _seed_monitoring_query_fixture()

        async with AsyncSessionLocal() as session:
            response = await query_monitoring_vendor(
                db=session,
                profile_id=fixture["profile_id"],
                vendor_id=fixture["openai_vendor_id"],
            )

        assert response.vendor_id == fixture["openai_vendor_id"]
        assert [item.model_config_id for item in response.models] == [
            fixture["openai_backup_model_id"],
            fixture["openai_primary_model_id"],
        ]
        assert [item.fused_status for item in response.models] == [
            "unhealthy",
            "degraded",
        ]
        assert [item.connection_count for item in response.models] == [1, 2]

    @pytest.mark.asyncio
    async def test_query_monitoring_model_returns_connection_rows_with_recent_history(
        self,
    ):
        queries_module = _load_module("app.services.monitoring.queries")
        query_monitoring_model = _require_attr(
            queries_module,
            "query_monitoring_model",
        )
        fixture = await _seed_monitoring_query_fixture()
        fixed_now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)

        async with AsyncSessionLocal() as session:
            session.add_all(
                [
                    MonitoringConnectionProbeResult(
                        profile_id=fixture["profile_id"],
                        vendor_id=fixture["openai_vendor_id"],
                        model_config_id=fixture["openai_primary_model_id"],
                        connection_id=fixture["primary_degraded_connection_id"],
                        endpoint_id=fixture["primary_degraded_endpoint_id"],
                        endpoint_ping_status="healthy",
                        endpoint_ping_ms=100 + minute_offset,
                        conversation_status="healthy",
                        conversation_delay_ms=200 + minute_offset,
                        failure_kind=None,
                        detail=f"older model-detail probe {minute_offset}",
                        checked_at=fixed_now - timedelta(minutes=minute_offset),
                    )
                    for minute_offset in range(3, 63)
                ]
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            response = await query_monitoring_model(
                db=session,
                profile_id=fixture["profile_id"],
                model_config_id=fixture["openai_primary_model_id"],
            )

        assert response.model_config_id == fixture["openai_primary_model_id"]
        assert response.model_id == fixture["openai_primary_model_key"]
        assert [row.connection_id for row in response.connections] == [
            fixture["primary_healthy_connection_id"],
            fixture["primary_degraded_connection_id"],
        ]
        required_model_detail_fields = {
            "last_probe_status",
            "last_probe_at",
            "endpoint_ping_status",
            "conversation_status",
            "fused_status",
            "recent_history",
        }
        for row in response.connections:
            row_payload = row.model_dump()
            assert required_model_detail_fields.issubset(row_payload)
            assert "availability_cells" not in row_payload
            assert row.last_probe_status in {"healthy", "degraded", "unhealthy"}
            assert row.last_probe_at is not None

        healthy_row = response.connections[0]
        assert healthy_row.last_probe_status == "healthy"
        assert healthy_row.last_probe_at == datetime(
            2026, 3, 29, 11, 59, tzinfo=timezone.utc
        )
        assert healthy_row.endpoint_ping_status == "healthy"
        assert healthy_row.monitoring_probe_interval_seconds == 180
        assert healthy_row.endpoint_ping_ms == 82
        assert healthy_row.conversation_status == "healthy"
        assert healthy_row.conversation_delay_ms == 145
        assert healthy_row.fused_status == "healthy"
        assert [item.endpoint_ping_ms for item in healthy_row.recent_history] == [
            82,
            84,
        ]
        assert [item.checked_at for item in healthy_row.recent_history] == sorted(
            [item.checked_at for item in healthy_row.recent_history],
            reverse=True,
        )

        degraded_row = response.connections[1]
        assert degraded_row.last_probe_status == "degraded"
        assert degraded_row.last_probe_at == datetime(
            2026, 3, 29, 11, 58, tzinfo=timezone.utc
        )
        assert degraded_row.endpoint_ping_status == "healthy"
        assert degraded_row.monitoring_probe_interval_seconds == 240
        assert degraded_row.endpoint_ping_ms == 115
        assert degraded_row.conversation_status == "unhealthy"
        assert degraded_row.conversation_delay_ms is None
        assert degraded_row.fused_status == "degraded"
        assert len(degraded_row.recent_history) == 60
        assert [item.checked_at for item in degraded_row.recent_history] == sorted(
            [item.checked_at for item in degraded_row.recent_history],
            reverse=True,
        )
        assert degraded_row.recent_history[0].checked_at == fixed_now - timedelta(
            minutes=2
        )
        assert degraded_row.recent_history[-1].checked_at == fixed_now - timedelta(
            minutes=61
        )
        assert degraded_row.recent_history[0].endpoint_ping_ms == 115
        assert degraded_row.recent_history[-1].endpoint_ping_ms == 161
        assert degraded_row.recent_history[0].conversation_status == "unhealthy"
        assert degraded_row.recent_history[0].conversation_delay_ms is None
        assert degraded_row.recent_history[0].failure_kind == "timeout"
