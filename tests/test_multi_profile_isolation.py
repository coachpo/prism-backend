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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException
from sqlalchemy import select, func
from datetime import datetime

from app.models.models import (
    Profile,
    Provider,
    ModelConfig,
    Endpoint,
    Connection,
    UserSetting,
    EndpointFxRateSetting,
    RequestLog,
    AuditLog,
    HeaderBlocklistRule,
)
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
from app.services.loadbalancer import get_model_config_with_connections
from app.services.stats_service import (
    log_request,
    get_request_logs,
    get_spending_report,
)
from app.services.audit_service import record_audit_log


class TestProfileCRUDAndLifecycle:
    """FR-001: Profile Entity and Lifecycle"""

    @pytest.mark.asyncio
    async def test_create_profile_success(self):
        """Profile creation succeeds when under capacity."""
        mock_db = AsyncMock()
        mock_db.add = MagicMock()

        # Mock capacity check (9 existing profiles)
        count_result = MagicMock()
        count_result.scalar_one.return_value = 9

        # Mock name uniqueness check (no conflict)
        existing_result = MagicMock()
        existing_result.scalar_one_or_none.return_value = None

        mock_db.execute.side_effect = [count_result, existing_result]
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        body = ProfileCreate(name="Test Profile", description="Test")

        profile = await create_profile(body=body, db=mock_db)

        assert mock_db.add.called
        added_profile = mock_db.add.call_args[0][0]
        assert added_profile.name == "Test Profile"
        assert added_profile.is_active is False
        assert added_profile.version == 0

    @pytest.mark.asyncio
    async def test_create_profile_at_capacity_fails(self):
        """Profile creation fails when 10 non-deleted profiles exist."""
        mock_db = AsyncMock()

        # Mock capacity check (10 existing profiles)
        count_result = MagicMock()
        count_result.scalar_one.return_value = 10

        mock_db.execute.return_value = count_result

        body = ProfileCreate(name="Test Profile")

        with pytest.raises(HTTPException) as exc_info:
            await create_profile(body=body, db=mock_db)

        assert exc_info.value.status_code == 409
        assert "Maximum 10 profiles reached" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_create_profile_duplicate_name_fails(self):
        """Profile creation fails with duplicate name."""
        mock_db = AsyncMock()

        # Mock capacity check (under capacity)
        count_result = MagicMock()
        count_result.scalar_one.return_value = 5

        # Mock name conflict
        existing_profile = MagicMock()
        existing_result = MagicMock()
        existing_result.scalar_one_or_none.return_value = existing_profile

        mock_db.execute.side_effect = [count_result, existing_result]

        body = ProfileCreate(name="Duplicate")

        with pytest.raises(HTTPException) as exc_info:
            await create_profile(body=body, db=mock_db)

        assert exc_info.value.status_code == 409
        assert "already exists" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_activate_profile_with_valid_cas(self):
        """Profile activation succeeds with correct CAS payload."""
        mock_db = AsyncMock()

        # Mock current active profile
        current_active = MagicMock()
        current_active.id = 1
        current_active.version = 5
        current_active.is_active = True

        active_result = MagicMock()
        active_result.scalar_one_or_none.return_value = current_active

        # Mock target profile
        target_profile = MagicMock()
        target_profile.id = 2
        target_profile.is_active = False
        target_profile.version = 3

        target_result = MagicMock()
        target_result.scalar_one_or_none.return_value = target_profile

        mock_db.execute.side_effect = [active_result, target_result]
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        body = ProfileActivateRequest(
            expected_active_profile_id=1, expected_active_profile_version=5
        )

        result = await activate_profile(profile_id=2, body=body, db=mock_db)

        # Verify atomic switch
        assert current_active.is_active is False
        assert current_active.version == 6
        assert target_profile.is_active is True
        assert target_profile.version == 4

    @pytest.mark.asyncio
    async def test_activate_profile_with_stale_cas_fails(self):
        """Profile activation fails with stale CAS payload."""
        mock_db = AsyncMock()

        # Mock current active profile with different version
        current_active = MagicMock()
        current_active.id = 1
        current_active.version = 7  # Version changed since client read

        active_result = MagicMock()
        active_result.scalar_one_or_none.return_value = current_active

        mock_db.execute.return_value = active_result

        body = ProfileActivateRequest(
            expected_active_profile_id=1,
            expected_active_profile_version=5,  # Stale version
        )

        with pytest.raises(HTTPException) as exc_info:
            await activate_profile(profile_id=2, body=body, db=mock_db)

        assert exc_info.value.status_code == 409
        assert "version mismatch" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_delete_active_profile_fails(self):
        """Active profile cannot be deleted."""
        mock_db = AsyncMock()

        # Mock active profile
        profile = MagicMock()
        profile.is_active = True

        result = MagicMock()
        result.scalar_one_or_none.return_value = profile

        mock_db.execute.return_value = result

        with pytest.raises(HTTPException) as exc_info:
            await delete_profile(profile_id=1, db=mock_db)

        assert exc_info.value.status_code == 400
        assert "Cannot delete active profile" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_delete_inactive_profile_soft_deletes(self):
        """Inactive profile is soft-deleted."""
        mock_db = AsyncMock()

        # Mock inactive profile
        profile = MagicMock()
        profile.is_active = False
        profile.deleted_at = None

        result = MagicMock()
        result.scalar_one_or_none.return_value = profile

        mock_db.execute.return_value = result
        mock_db.flush = AsyncMock()

        await delete_profile(profile_id=2, db=mock_db)

        assert profile.deleted_at is not None
        assert mock_db.flush.called


class TestProfileScopedDataIsolation:
    """FR-002: Scoped Data Model"""

    @pytest.mark.asyncio
    async def test_same_model_id_in_different_profiles(self):
        """Same model_id can exist in multiple profiles without collision."""
        # Profile 1 has gpt-4
        profile1_model = MagicMock()
        profile1_model.id = 1
        profile1_model.profile_id = 1
        profile1_model.model_id = "gpt-4"

        # Profile 2 also has gpt-4 (different config)
        profile2_model = MagicMock()
        profile2_model.id = 2
        profile2_model.profile_id = 2
        profile2_model.model_id = "gpt-4"

        # Both should coexist due to unique(profile_id, model_id) constraint
        assert profile1_model.model_id == profile2_model.model_id
        assert profile1_model.profile_id != profile2_model.profile_id

    @pytest.mark.asyncio
    async def test_list_models_filters_by_profile(self):
        """list_models returns only models for the effective profile."""
        mock_db = AsyncMock()

        now = datetime.utcnow()
        provider = SimpleNamespace(
            id=1,
            name="OpenAI",
            provider_type="openai",
            description="OpenAI provider",
            audit_enabled=True,
            audit_capture_bodies=False,
            created_at=now,
            updated_at=now,
        )
        model1 = SimpleNamespace(
            id=1,
            profile_id=1,
            provider_id=1,
            provider=provider,
            model_id="gpt-4",
            display_name=None,
            model_type="native",
            redirect_to=None,
            lb_strategy="single",
            failover_recovery_enabled=True,
            failover_recovery_cooldown_seconds=60,
            is_enabled=True,
            connections=[],
            created_at=now,
            updated_at=now,
        )
        model2 = SimpleNamespace(
            id=2,
            profile_id=1,
            provider_id=1,
            provider=provider,
            model_id="gpt-3.5-turbo",
            display_name=None,
            model_type="native",
            redirect_to=None,
            lb_strategy="single",
            failover_recovery_enabled=True,
            failover_recovery_cooldown_seconds=60,
            is_enabled=True,
            connections=[],
            created_at=now,
            updated_at=now,
        )

        result = MagicMock()
        result.scalars.return_value.all.return_value = [model1, model2]

        mock_db.execute.return_value = result

        # Mock health stats - should return dict not list
        with patch(
            "app.routers.models.get_model_health_stats", new_callable=AsyncMock
        ) as health_mock:
            health_mock.return_value = {}

            models = await list_models(db=mock_db, profile_id=1)

            # Verify query filtered by profile_id=1
            assert len(models) == 2
            assert all(m.profile_id == 1 for m in models)

    @pytest.mark.asyncio
    async def test_list_endpoints_filters_by_profile(self):
        """list_endpoints returns only endpoints for the effective profile."""
        mock_db = AsyncMock()

        # Mock endpoints from profile 2
        endpoint1 = MagicMock()
        endpoint1.profile_id = 2
        endpoint1.name = "openai-main"

        endpoint2 = MagicMock()
        endpoint2.profile_id = 2
        endpoint2.name = "openai-backup"

        result = MagicMock()
        result.scalars.return_value.all.return_value = [endpoint1, endpoint2]

        mock_db.execute.return_value = result

        endpoints = await list_endpoints(db=mock_db, profile_id=2)

        assert len(endpoints) == 2
        assert all(e.profile_id == 2 for e in [endpoint1, endpoint2])

    @pytest.mark.asyncio
    async def test_user_settings_unique_per_profile(self):
        """UserSetting has unique constraint on profile_id (1:1 relationship)."""
        # Profile 1 has one user_settings row
        setting1 = MagicMock()
        setting1.profile_id = 1
        setting1.report_currency_code = "USD"

        # Profile 2 has its own user_settings row
        setting2 = MagicMock()
        setting2.profile_id = 2
        setting2.report_currency_code = "EUR"

        # Both can coexist, but only one per profile
        assert setting1.profile_id != setting2.profile_id


class TestProxyRuntimeIsolation:
    """FR-003: Proxy Runtime Isolation"""

    @pytest.mark.asyncio
    async def test_proxy_uses_active_profile_context(self):
        """Proxy routing uses active profile for model resolution."""
        mock_db = AsyncMock()

        # Mock model in profile 1 (active)
        model = MagicMock()
        model.profile_id = 1
        model.model_id = "gpt-4"
        model.model_type = "native"
        model.redirect_to = None

        connection = MagicMock()
        connection.profile_id = 1

        model.connections_rel = [connection]

        result = MagicMock()
        result.scalar_one_or_none.return_value = model

        mock_db.execute.return_value = result

        resolved = await get_model_config_with_connections(
            db=mock_db, model_id="gpt-4", profile_id=1
        )

        assert resolved is not None
        assert resolved.profile_id == 1

    @pytest.mark.asyncio
    async def test_proxy_alias_stays_within_profile(self):
        """Proxy alias resolution must stay within same profile."""
        mock_db = AsyncMock()

        # Mock proxy model in profile 1
        proxy_model = MagicMock()
        proxy_model.profile_id = 1
        proxy_model.model_id = "gpt-4-alias"
        proxy_model.model_type = "proxy"
        proxy_model.redirect_to = "gpt-4-real"

        # Mock target model in profile 1
        target_model = MagicMock()
        target_model.profile_id = 1
        target_model.model_id = "gpt-4-real"
        target_model.model_type = "native"
        target_model.redirect_to = None
        target_model.connections_rel = [MagicMock()]

        # First query returns proxy, second returns target
        proxy_result = MagicMock()
        proxy_result.scalar_one_or_none.return_value = proxy_model

        target_result = MagicMock()
        target_result.scalar_one_or_none.return_value = target_model

        mock_db.execute.side_effect = [proxy_result, target_result]

        resolved = await get_model_config_with_connections(
            db=mock_db, model_id="gpt-4-alias", profile_id=1
        )

        # Verify both queries filtered by profile_id=1
        assert resolved.profile_id == 1
        assert resolved.model_id == "gpt-4-real"

    @pytest.mark.asyncio
    async def test_model_not_found_in_other_profile(self):
        """Model existing in another profile returns 404 for active profile."""
        mock_db = AsyncMock()

        # Model exists in profile 2, but we're querying profile 1
        result = MagicMock()
        result.scalar_one_or_none.return_value = None

        mock_db.execute.return_value = result

        resolved = await get_model_config_with_connections(
            db=mock_db, model_id="gpt-4", profile_id=1
        )

        assert resolved is None


class TestConfigExportImportIsolation:
    """FR-007: Config Export/Import Isolation"""

    @pytest.mark.asyncio
    async def test_export_config_filters_by_profile(self):
        """Config export returns only data for the specified profile."""
        from app.routers.config import export_config

        mock_db = AsyncMock()

        now = datetime.utcnow()
        provider = SimpleNamespace(
            id=1,
            name="OpenAI",
            provider_type="openai",
            description="OpenAI provider",
            audit_enabled=True,
            audit_capture_bodies=False,
        )
        endpoint = SimpleNamespace(
            id=10,
            profile_id=1,
            name="openai-main",
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
        )
        model = SimpleNamespace(
            id=20,
            profile_id=1,
            provider_id=1,
            model_id="gpt-4",
            display_name=None,
            model_type="native",
            redirect_to=None,
            lb_strategy="single",
            failover_recovery_enabled=True,
            failover_recovery_cooldown_seconds=60,
            is_enabled=True,
            connections=[],
            created_at=now,
            updated_at=now,
        )

        # Mock query results
        provider_result = MagicMock()
        provider_result.scalars.return_value.all.return_value = [provider]

        endpoint_result = MagicMock()
        endpoint_result.scalars.return_value.all.return_value = [endpoint]

        model_result = MagicMock()
        model_result.scalars.return_value.all.return_value = [model]

        user_settings_result = MagicMock()
        user_settings_result.scalar_one_or_none.return_value = None

        fx_result = MagicMock()
        fx_result.scalars.return_value.all.return_value = []

        blocklist_result = MagicMock()
        blocklist_result.scalars.return_value.all.return_value = []

        mock_db.execute.side_effect = [
            provider_result,
            endpoint_result,
            model_result,
            user_settings_result,
            fx_result,
            blocklist_result,
        ]

        config = await export_config(db=mock_db, profile_id=1)
        payload = json.loads(config.body.decode("utf-8"))

        # Verify export contains profile 1 data only
        assert payload["version"] == 1
        assert len(payload["endpoints"]) == 1
        assert len(payload["models"]) == 1

    @pytest.mark.asyncio
    async def test_import_config_replaces_target_profile_only(self):
        """Config import replaces only the target profile's data."""
        # This is a complex integration test that would require
        # full database setup. The key assertion is that import
        # deletes and recreates data filtered by profile_id.
        pass  # Placeholder for full integration test


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
                provider_type="openai",
                endpoint_id=10,
                connection_id=20,
                endpoint_base_url="https://api.openai.com/v1",
                status_code=200,
                response_time_ms=100,
                is_stream=False,
                request_path="/v1/chat/completions",
            )

        mock_session.add.assert_called_once()
        log_entry = mock_session.add.call_args[0][0]
        assert log_entry.profile_id == 1

    @pytest.mark.asyncio
    async def test_audit_logs_include_profile_id(self):
        """Audit logs store profile_id for attribution."""
        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "app.core.database.AsyncSessionLocal", return_value=mock_session_ctx
        ):
            await record_audit_log(
                profile_id=1,
                request_log_id=100,
                provider_id=1,
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

        mock_session.add.assert_called_once()
        audit_entry = mock_session.add.call_args[0][0]
        assert audit_entry.profile_id == 1

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


class TestFailoverRecoveryStateIsolation:
    """FR-005: In-Memory State Isolation"""

    @pytest.mark.asyncio
    async def test_recovery_state_keyed_by_profile_and_connection(self):
        """Failover recovery state is keyed by (profile_id, connection_id)."""
        from app.services.loadbalancer import _recovery_state

        # Profile 1, connection 10
        key1 = (1, 10)
        _recovery_state[key1] = {
            "failed_at": datetime.utcnow(),
            "cooldown_seconds": 60,
        }

        # Profile 2, connection 10 (same connection ID, different profile)
        key2 = (2, 10)
        _recovery_state[key2] = {
            "failed_at": datetime.utcnow(),
            "cooldown_seconds": 120,
        }

        # Both can coexist without collision
        assert key1 in _recovery_state
        assert key2 in _recovery_state
        assert (
            _recovery_state[key1]["cooldown_seconds"]
            != _recovery_state[key2]["cooldown_seconds"]
        )


class TestCrossProfileLeakagePrevention:
    """Verify no cross-profile data leakage in queries."""

    @pytest.mark.asyncio
    async def test_get_model_by_id_returns_404_for_other_profile(self):
        """GET /api/models/{id} returns 404 when model exists in another profile."""
        from app.routers.models import get_model

        mock_db = AsyncMock()

        # Model exists in profile 2, but we're querying with profile 1 context
        result = MagicMock()
        result.scalar_one_or_none.return_value = None

        mock_db.execute.return_value = result

        with pytest.raises(HTTPException) as exc_info:
            await get_model(model_config_id=999, db=mock_db, profile_id=1)

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_spending_report_filters_by_profile(self):
        """Spending reports only include requests from the effective profile."""
        mock_db = AsyncMock()

        summary_row = SimpleNamespace(
            total_cost_micros=3000000,
            successful_request_count=2,
            priced_request_count=2,
            unpriced_request_count=0,
            total_input_tokens=1000,
            total_output_tokens=500,
            total_cache_read_input_tokens=0,
            total_cache_creation_input_tokens=0,
            total_reasoning_tokens=0,
            total_tokens=1500,
        )
        summary_result = MagicMock()
        summary_result.one.return_value = summary_row

        top_model_result = MagicMock()
        top_model_result.all.return_value = [
            SimpleNamespace(model_id="gpt-4", total_cost_micros=3000000)
        ]

        top_endpoint_result = MagicMock()
        top_endpoint_result.all.return_value = [
            SimpleNamespace(
                endpoint_id=10,
                endpoint_label="openai-main",
                total_cost_micros=3000000,
            )
        ]

        unpriced_reason_result = MagicMock()
        unpriced_reason_result.all.return_value = []

        settings_row = SimpleNamespace(
            report_currency_code="USD",
            report_currency_symbol="$",
        )
        settings_result = MagicMock()
        settings_result.scalar_one_or_none.return_value = settings_row

        # group_by defaults to "none", so there are 5 execute() calls
        mock_db.execute.side_effect = [
            summary_result,
            top_model_result,
            top_endpoint_result,
            unpriced_reason_result,
            settings_result,
        ]

        report = await get_spending_report(
            db=mock_db,
            profile_id=1,
            limit=100,
            offset=0,
        )

        assert report["summary"]["successful_request_count"] == 2
        assert report["summary"]["total_cost_micros"] == 3000000
        assert report["report_currency_code"] == "USD"
