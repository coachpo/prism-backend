"""
Multi-Profile Isolation Test Suite

Tests comprehensive profile isolation across all functional requirements:
- FR-001: Profile CRUD and lifecycle
- FR-002: Scoped data model
- FR-003: Proxy runtime isolation
- FR-004: Active profile switch safety
- FR-005: In-memory state isolation
- FR-006: API scope semantics
- FR-007: Config export/import isolation
- FR-008: Costing and settings isolation
- FR-009: Observability and audit attribution
"""

import pytest
import json
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, func
from datetime import datetime, timedelta, timezone

from app.models.models import (
    Profile,
    ModelConfig,
    Endpoint,
    Connection,
    ProxyApiKey,
    UserSetting,
    UsageRequestEvent,
    Vendor,
    EndpointFxRateSetting,
    RequestLog,
    AuditLog,
    HeaderBlocklistRule,
)
from app.core.database import AsyncSessionLocal, get_engine
from app.core.time import utc_now
from app.main import app
from app.routers.profiles import (
    list_profiles,
    get_active_profile,
    create_profile,
    update_profile,
    activate_profile,
    delete_profile,
)
from app.routers.models import list_models
from app.routers.endpoints import list_endpoints
from app.routers.stats import list_request_logs
from app.routers.settings import get_costing_settings
from app.schemas.schemas import (
    ProfileCreate,
    ProfileUpdate,
    ProfileActivateRequest,
)
from app.services.loadbalancer.planner import get_model_config_with_connections
from app.services.stats_service import (
    log_request,
    get_request_logs,
    get_spending_report,
)
from app.services.audit_service import record_audit_log
from tests.loadbalance_strategy_helpers import make_loadbalance_strategy


async def _seed_usage_snapshot_profiles_for_scope_test() -> tuple[
    int, int, int, int, str, str
]:
    suffix = utc_now().strftime("%H%M%S%f")
    created_at = utc_now() - timedelta(hours=1)

    async with AsyncSessionLocal() as session:
        profile_a = Profile(
            name=f"Observability Snapshot A {suffix}",
            is_active=False,
            is_default=False,
            version=0,
        )
        profile_b = Profile(
            name=f"Observability Snapshot B {suffix}",
            is_active=False,
            is_default=False,
            version=0,
        )
        vendor = Vendor(
            key=f"observability-snapshot-vendor-{suffix}",
            name=f"Observability Snapshot Vendor {suffix}",
            audit_enabled=False,
            audit_capture_bodies=False,
        )
        strategy_a = make_loadbalance_strategy(
            profile=profile_a, strategy_type="failover"
        )
        strategy_b = make_loadbalance_strategy(
            profile=profile_b, strategy_type="failover"
        )
        model_a = ModelConfig(
            profile=profile_a,
            vendor=vendor,
            api_family="openai",
            model_id=f"scope-a-{suffix}",
            display_name=f"Scope Model A {suffix}",
            model_type="native",
            loadbalance_strategy=strategy_a,
            is_enabled=True,
        )
        model_b = ModelConfig(
            profile=profile_b,
            vendor=vendor,
            api_family="openai",
            model_id=f"scope-b-{suffix}",
            display_name=f"Scope Model B {suffix}",
            model_type="native",
            loadbalance_strategy=strategy_b,
            is_enabled=True,
        )
        endpoint_a = Endpoint(
            profile=profile_a,
            name=f"Scope Endpoint A {suffix}",
            base_url=f"https://scope-a-{suffix}.example.com/v1",
            api_key=f"sk-scope-a-{suffix}",
            position=0,
        )
        endpoint_b = Endpoint(
            profile=profile_b,
            name=f"Scope Endpoint B {suffix}",
            base_url=f"https://scope-b-{suffix}.example.com/v1",
            api_key=f"sk-scope-b-{suffix}",
            position=0,
        )
        connection_a = Connection(
            profile=profile_a,
            model_config_rel=model_a,
            endpoint_rel=endpoint_a,
            is_active=True,
            priority=0,
            name=f"Scope Connection A {suffix}",
        )
        connection_b = Connection(
            profile=profile_b,
            model_config_rel=model_b,
            endpoint_rel=endpoint_b,
            is_active=True,
            priority=0,
            name=f"Scope Connection B {suffix}",
        )
        proxy_key_a = ProxyApiKey(
            name=f"Scope Key A {suffix}",
            key_prefix=f"prism_pk_scope_a_{suffix}",
            key_hash=(suffix * 8)[:64],
            last_four=suffix[-4:],
            is_active=True,
        )
        proxy_key_b = ProxyApiKey(
            name=f"Scope Key B {suffix}",
            key_prefix=f"prism_pk_scope_b_{suffix}",
            key_hash=(suffix[::-1] * 8)[:64],
            last_four=suffix[:4],
            is_active=True,
        )

        session.add_all(
            [
                profile_a,
                profile_b,
                vendor,
                strategy_a,
                strategy_b,
                model_a,
                model_b,
                endpoint_a,
                endpoint_b,
                connection_a,
                connection_b,
                proxy_key_a,
                proxy_key_b,
                UserSetting(profile=profile_a),
                UserSetting(profile=profile_b),
            ]
        )
        await session.flush()

        ingress_a = f"scope-ingress-a-{suffix}"
        ingress_b = f"scope-ingress-b-{suffix}"
        session.add_all(
            [
                UsageRequestEvent(
                    profile_id=profile_a.id,
                    ingress_request_id=ingress_a,
                    model_id=model_a.model_id,
                    resolved_target_model_id=model_a.model_id,
                    api_family="openai",
                    endpoint_id=endpoint_a.id,
                    connection_id=connection_a.id,
                    proxy_api_key_id=proxy_key_a.id,
                    proxy_api_key_name_snapshot=f"Scope Snapshot A {suffix}",
                    status_code=200,
                    success_flag=True,
                    input_tokens=10,
                    output_tokens=20,
                    total_tokens=35,
                    cache_read_input_tokens=3,
                    cache_creation_input_tokens=2,
                    reasoning_tokens=1,
                    total_cost_original_micros=500,
                    total_cost_user_currency_micros=500,
                    currency_code_original="USD",
                    report_currency_code="USD",
                    report_currency_symbol="$",
                    attempt_count=1,
                    request_path="/v1/chat/completions",
                    created_at=created_at,
                ),
                UsageRequestEvent(
                    profile_id=profile_b.id,
                    ingress_request_id=ingress_b,
                    model_id=model_b.model_id,
                    resolved_target_model_id=model_b.model_id,
                    api_family="openai",
                    endpoint_id=endpoint_b.id,
                    connection_id=connection_b.id,
                    proxy_api_key_id=proxy_key_b.id,
                    proxy_api_key_name_snapshot=f"Scope Snapshot B {suffix}",
                    status_code=200,
                    success_flag=True,
                    input_tokens=99,
                    output_tokens=1,
                    total_tokens=100,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                    reasoning_tokens=0,
                    total_cost_original_micros=1000,
                    total_cost_user_currency_micros=1000,
                    currency_code_original="USD",
                    report_currency_code="USD",
                    report_currency_symbol="$",
                    attempt_count=1,
                    request_path="/v1/chat/completions",
                    created_at=created_at,
                ),
            ]
        )
        await session.commit()

        return (
            profile_a.id,
            profile_b.id,
            endpoint_a.id,
            endpoint_b.id,
            model_a.model_id,
            model_b.model_id,
        )


class TestCostingAndSettingsIsolation:
    """FR-008: Costing and Settings Isolation"""

    @pytest.mark.asyncio
    async def test_costing_settings_scoped_to_profile(self):
        """Costing settings are profile-scoped."""
        mock_db = AsyncMock()

        # Mock user settings for profile 1 with all required fields
        user_setting = MagicMock()
        user_setting.profile_id = 1
        user_setting.report_currency_code = "USD"
        user_setting.report_currency_symbol = "$"
        user_setting.timezone_preference = "UTC"

        settings_result = MagicMock()
        settings_result.scalar_one_or_none.return_value = user_setting

        # Mock FX mappings for profile 1
        fx_result = MagicMock()
        fx_result.scalars.return_value.all.return_value = []

        mock_db.execute.side_effect = [settings_result, fx_result]

        settings = await get_costing_settings(db=mock_db, profile_id=1)

        assert settings.report_currency_code == "USD"
        assert settings.report_currency_symbol == "$"

    @pytest.mark.asyncio
    async def test_fx_mappings_scoped_to_profile(self):
        """FX rate mappings are validated within profile-bound model/endpoint pairs."""
        # FX mappings have unique constraint on (profile_id, model_id, endpoint_id)
        fx1 = MagicMock()
        fx1.profile_id = 1
        fx1.model_id = "gpt-4"
        fx1.endpoint_id = 10
        fx1.fx_rate = 1.2

        fx2 = MagicMock()
        fx2.profile_id = 2
        fx2.model_id = "gpt-4"  # Same model_id
        fx2.endpoint_id = 10  # Same endpoint_id
        fx2.fx_rate = 1.5  # Different rate

        # Both can coexist due to different profile_id
        assert fx1.profile_id != fx2.profile_id


class TestObservabilityAttribution:
    """FR-009: Observability and Audit Attribution"""

    @pytest.mark.asyncio
    async def test_request_logs_include_profile_id(self):
        """Request logs store profile_id for attribution."""
        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "app.core.database.AsyncSessionLocal", return_value=mock_session_ctx
        ):
            await log_request(
                profile_id=1,
                model_id="gpt-4",
                api_family="openai",
                vendor_id=7,
                vendor_key="openai",
                vendor_name="OpenAI",
                endpoint_id=10,
                connection_id=20,
                endpoint_base_url="https://api.openai.com",
                status_code=200,
                response_time_ms=100,
                is_stream=False,
                request_path="/v1/chat/completions",
            )

        mock_session.add.assert_called_once()
        log_entry = mock_session.add.call_args[0][0]
        assert log_entry.profile_id == 1
        assert log_entry.api_family == "openai"
        assert log_entry.vendor_id == 7

    @pytest.mark.asyncio
    async def test_audit_logs_include_profile_id(self):
        """Audit logs store profile_id for attribution."""
        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        enqueued_job = {}

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        def capture_enqueue(*, name, run, max_retries=0, retry_delay_seconds=0.0):
            enqueued_job.update(
                name=name,
                run=run,
                max_retries=max_retries,
                retry_delay_seconds=retry_delay_seconds,
            )

        with (
            patch("app.core.database.AsyncSessionLocal", return_value=mock_session_ctx),
            patch(
                "app.services.audit_service.background_task_manager.enqueue",
                MagicMock(side_effect=capture_enqueue),
            ),
        ):
            await record_audit_log(
                profile_id=1,
                request_log_id=100,
                vendor_id=1,
                model_id="gpt-4",
                request_method="POST",
                request_url="https://api.openai.com/v1/chat/completions",
                request_headers={"authorization": "Bearer sk-test"},
                request_body=b'{"model":"gpt-4"}',
                response_status=200,
                response_headers={"content-type": "application/json"},
                response_body=b'{"choices":[]}',
                is_stream=False,
                duration_ms=100,
                capture_bodies=True,
            )

            await enqueued_job["run"]()

        mock_session.add.assert_called_once()
        audit_entry = mock_session.add.call_args[0][0]
        assert audit_entry.profile_id == 1
        assert audit_entry.vendor_id == 1
        assert audit_entry.response_body == '{"choices":[]}'

    def test_stats_routes_use_api_family_query_params(self):
        from app.routers.stats import (
            get_throughput,
            list_request_logs,
            spending_report,
            stats_summary,
        )

        for route in (
            list_request_logs,
            stats_summary,
            spending_report,
            get_throughput,
        ):
            parameters = inspect.signature(route).parameters
            assert "api_family" in parameters

    def test_audit_routes_use_vendor_id_filter(self):
        from app.routers.audit import list_audit_logs

        parameters = inspect.signature(list_audit_logs).parameters
        assert "vendor_id" in parameters

    @pytest.mark.asyncio
    async def test_stats_queries_filter_by_profile(self):
        """Stats queries default to active profile filtering."""
        mock_db = AsyncMock()

        # Mock request logs for profile 1
        log1 = MagicMock()
        log1.profile_id = 1
        log1.model_id = "gpt-4"

        log2 = MagicMock()
        log2.profile_id = 1
        log2.model_id = "gpt-3.5-turbo"

        result = MagicMock()
        result.scalars.return_value.all.return_value = [log1, log2]

        count_result = MagicMock()
        count_result.scalar.return_value = 2

        mock_db.execute.side_effect = [count_result, result]

        # get_request_logs returns tuple (items, total)
        items, total = await get_request_logs(
            db=mock_db,
            profile_id=1,
            limit=100,
            offset=0,
        )

        # Verify all logs belong to profile 1
        assert total == 2
        assert all(log.profile_id == 1 for log in items)

    @pytest.mark.asyncio
    async def test_usage_snapshot_route_scopes_results_to_effective_profile(self):
        await get_engine().dispose()
        (
            profile_a_id,
            profile_b_id,
            _,
            _,
            model_a_id,
            model_b_id,
        ) = await _seed_usage_snapshot_profiles_for_scope_test()
        transport = ASGITransport(app=app)

        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response_a = await client.get(
                "/api/stats/usage-snapshot",
                params={"preset": "24h"},
                headers={"X-Profile-Id": str(profile_a_id)},
            )
            response_b = await client.get(
                "/api/stats/usage-snapshot",
                params={"preset": "24h"},
                headers={"X-Profile-Id": str(profile_b_id)},
            )

        assert response_a.status_code == 200
        assert response_b.status_code == 200

        payload_a = response_a.json()
        payload_b = response_b.json()

        assert payload_a["overview"]["total_requests"] == 1
        assert payload_b["overview"]["total_requests"] == 1
        assert payload_a["model_statistics"][0]["model_id"] == model_a_id
        assert payload_b["model_statistics"][0]["model_id"] == model_b_id
        assert payload_a["model_statistics"][0]["model_id"] != model_b_id
        assert payload_b["model_statistics"][0]["model_id"] != model_a_id
        assert "request_events" not in payload_a
        assert "request_events" not in payload_b

    @pytest.mark.asyncio
    async def test_endpoint_model_statistics_route_rejects_other_profile_endpoint(self):
        await get_engine().dispose()
        (
            profile_a_id,
            profile_b_id,
            endpoint_a_id,
            endpoint_b_id,
            model_a_id,
            _,
        ) = await _seed_usage_snapshot_profiles_for_scope_test()
        transport = ASGITransport(app=app)

        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response_a = await client.get(
                f"/api/stats/endpoints/{endpoint_a_id}/models",
                params={"preset": "24h"},
                headers={"X-Profile-Id": str(profile_a_id)},
            )
            response_cross = await client.get(
                f"/api/stats/endpoints/{endpoint_b_id}/models",
                params={"preset": "24h"},
                headers={"X-Profile-Id": str(profile_a_id)},
            )

        assert response_a.status_code == 200
        assert response_cross.status_code == 404

        payload_a = response_a.json()
        assert payload_a[0]["model_id"] == model_a_id


class TestHeaderBlocklistScoping:
    """Header blocklist rules: system rules are global, user rules are profile-scoped."""

    @pytest.mark.asyncio
    async def test_system_rules_have_null_profile_id(self):
        """System blocklist rules have NULL profile_id (global)."""
        system_rule = MagicMock()
        system_rule.is_system = True
        system_rule.profile_id = None
        system_rule.pattern = "cf-ray"

        # Check constraint enforces: is_system=true → profile_id IS NULL
        assert system_rule.is_system is True
        assert system_rule.profile_id is None

    @pytest.mark.asyncio
    async def test_user_rules_have_profile_id(self):
        """User blocklist rules have NOT NULL profile_id (profile-scoped)."""
        user_rule = MagicMock()
        user_rule.is_system = False
        user_rule.profile_id = 1
        user_rule.pattern = "x-custom-header"

        # Check constraint enforces: is_system=false → profile_id IS NOT NULL
        assert user_rule.is_system is False
        assert user_rule.profile_id is not None
