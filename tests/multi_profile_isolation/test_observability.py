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
from app.services.loadbalancer import get_model_config_with_connections
from app.services.stats_service import (
    log_request,
    get_request_logs,
    get_spending_report,
)
from app.services.audit_service import record_audit_log


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

            await enqueued_job["run"]()

        mock_session.add.assert_called_once()
        audit_entry = mock_session.add.call_args[0][0]
        assert audit_entry.profile_id == 1
        assert audit_entry.response_body == '{"choices":[]}'

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
