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
        with patch(
            "app.routers.profiles.ensure_profile_invariants",
            new_callable=AsyncMock,
        ) as invariants_mock:
            invariants_mock.return_value = MagicMock()
            await create_profile(body=body, db=mock_db)

        assert mock_db.add.called
        added_profile = next(
            obj
            for obj in (call.args[0] for call in mock_db.add.call_args_list)
            if isinstance(obj, Profile)
        )
        assert added_profile.name == "Test Profile"
        assert added_profile.is_active is False
        assert added_profile.version == 0
        assert added_profile.is_default is False
        assert added_profile.is_editable is True

    @pytest.mark.asyncio
    async def test_create_profile_seeds_default_user_settings(self):
        mock_db = AsyncMock()
        mock_db.add = MagicMock()

        count_result = MagicMock()
        count_result.scalar_one.return_value = 1

        existing_result = MagicMock()
        existing_result.scalar_one_or_none.return_value = None

        mock_db.execute.side_effect = [count_result, existing_result]
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        body = ProfileCreate(name="Seeded Profile", description="Settings seed check")
        with patch(
            "app.routers.profiles.ensure_profile_invariants",
            new_callable=AsyncMock,
        ) as invariants_mock:
            invariants_mock.return_value = MagicMock()
            await create_profile(body=body, db=mock_db)

        added_objects = [call.args[0] for call in mock_db.add.call_args_list]
        added_profile = next(obj for obj in added_objects if isinstance(obj, Profile))
        added_settings = next(
            obj for obj in added_objects if isinstance(obj, UserSetting)
        )

        assert added_settings.profile is added_profile
        assert added_settings.report_currency_code == "USD"
        assert added_settings.report_currency_symbol == "$"
        assert added_settings.timezone_preference is None

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

        body = ProfileActivateRequest(expected_active_profile_id=1)
        with patch(
            "app.routers.profiles.ensure_profile_invariants",
            new_callable=AsyncMock,
        ) as invariants_mock:
            invariants_mock.return_value = current_active
            await activate_profile(profile_id=2, body=body, db=mock_db)

        # Verify atomic switch in two phases (deactivate flush -> activate flush)
        assert current_active.is_active is False
        assert current_active.version == 6
        assert target_profile.is_active is True
        assert target_profile.version == 4
        assert mock_db.flush.await_count == 2

    @pytest.mark.asyncio
    async def test_activate_profile_with_stale_active_id_fails(self):
        """Profile activation fails when expected active profile ID is stale."""
        mock_db = AsyncMock()

        # Mock current active profile with different version
        current_active = MagicMock()
        current_active.id = 1
        current_active.version = 7  # Version changed since client read

        active_result = MagicMock()
        active_result.scalar_one_or_none.return_value = current_active

        mock_db.execute.return_value = active_result

        body = ProfileActivateRequest(expected_active_profile_id=99)
        with patch(
            "app.routers.profiles.ensure_profile_invariants",
            new_callable=AsyncMock,
        ) as invariants_mock:
            invariants_mock.return_value = current_active
            with pytest.raises(HTTPException) as exc_info:
                await activate_profile(profile_id=2, body=body, db=mock_db)

        assert exc_info.value.status_code == 409
        assert "mismatch" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_activate_profile_conflict_returns_409(self):
        """IntegrityError during activation becomes 409 conflict."""
        mock_db = AsyncMock()

        current_active = MagicMock()
        current_active.id = 1
        current_active.version = 5
        current_active.is_active = True

        active_result = MagicMock()
        active_result.scalar_one_or_none.return_value = current_active

        target_profile = MagicMock()
        target_profile.id = 2
        target_profile.is_active = False
        target_profile.version = 3

        target_result = MagicMock()
        target_result.scalar_one_or_none.return_value = target_profile

        mock_db.execute.side_effect = [active_result, target_result]
        mock_db.flush = AsyncMock()
        from sqlalchemy.exc import IntegrityError

        mock_db.flush.side_effect = [
            None,
            IntegrityError("stmt", "params", Exception("dup")),
        ]

        body = ProfileActivateRequest(expected_active_profile_id=1)
        with patch(
            "app.routers.profiles.ensure_profile_invariants",
            new_callable=AsyncMock,
        ) as invariants_mock:
            invariants_mock.return_value = current_active
            with pytest.raises(HTTPException) as exc_info:
                await activate_profile(profile_id=2, body=body, db=mock_db)

        assert exc_info.value.status_code == 409
        assert "conflict" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_delete_active_profile_fails(self):
        """Active profile cannot be deleted."""
        mock_db = AsyncMock()

        # Mock active profile
        profile = MagicMock()
        profile.is_active = True
        profile.is_default = False

        result = MagicMock()
        result.scalar_one_or_none.return_value = profile

        mock_db.execute.return_value = result

        with pytest.raises(HTTPException) as exc_info:
            await delete_profile(profile_id=1, db=mock_db)

        assert exc_info.value.status_code == 400
        assert "Cannot delete active profile" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_delete_default_profile_fails(self):
        """Default profile cannot be deleted even when inactive."""
        mock_db = AsyncMock()

        profile = MagicMock()
        profile.is_default = True
        profile.is_active = False

        result = MagicMock()
        result.scalar_one_or_none.return_value = profile
        mock_db.execute.return_value = result

        with pytest.raises(HTTPException) as exc_info:
            await delete_profile(profile_id=1, db=mock_db)

        assert exc_info.value.status_code == 400
        assert "Default profile cannot be deleted" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_update_default_profile_name_fails(self):
        """Default profile name is immutable."""
        mock_db = AsyncMock()

        profile = MagicMock()
        profile.id = 1
        profile.name = "Default"
        profile.is_default = True
        profile.is_editable = True

        result = MagicMock()
        result.scalar_one_or_none.return_value = profile
        mock_db.execute.return_value = result

        with pytest.raises(HTTPException) as exc_info:
            await update_profile(
                profile_id=1,
                body=ProfileUpdate(name="Renamed Default"),
                db=mock_db,
            )

        assert exc_info.value.status_code == 400
        assert "immutable" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_delete_inactive_profile_soft_deletes(self):
        """Inactive profile is soft-deleted."""
        mock_db = AsyncMock()

        # Mock inactive profile
        profile = MagicMock()
        profile.is_active = False
        profile.deleted_at = None
        profile.is_default = False

        result = MagicMock()
        result.scalar_one_or_none.return_value = profile

        mock_db.execute.return_value = result
        mock_db.flush = AsyncMock()

        await delete_profile(profile_id=2, db=mock_db)

        assert profile.deleted_at is not None
        assert mock_db.flush.called
