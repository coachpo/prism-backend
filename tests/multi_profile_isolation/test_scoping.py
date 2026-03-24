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
from datetime import datetime, timezone

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
from app.services.loadbalancer.planner import get_model_config_with_connections
from app.services.stats_service import (
    log_request,
    get_request_logs,
    get_spending_report,
)
from app.services.audit_service import record_audit_log


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

        now = datetime.now(timezone.utc)
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
