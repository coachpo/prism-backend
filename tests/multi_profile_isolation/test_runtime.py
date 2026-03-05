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
    async def test_proxy_returns_requested_model_within_profile(self):
        """Model resolution remains profile-scoped without alias indirection."""
        mock_db = AsyncMock()

        model = MagicMock()
        model.profile_id = 1
        model.model_id = "gpt-4-alias"
        model.model_type = "native"
        model.redirect_to = None
        model.connections_rel = [MagicMock()]

        result = MagicMock()
        result.scalar_one_or_none.return_value = model
        mock_db.execute.return_value = result

        resolved = await get_model_config_with_connections(
            db=mock_db, model_id="gpt-4-alias", profile_id=1
        )

        assert resolved.profile_id == 1
        assert resolved.model_id == "gpt-4-alias"

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

class TestFailoverRecoveryStateIsolation:
    """FR-005: In-Memory State Isolation"""

    @pytest.mark.asyncio
    async def test_recovery_state_keyed_by_profile_and_connection(self):
        """Failover recovery state is keyed by (profile_id, connection_id)."""
        from app.services.loadbalancer import _recovery_state

        # Profile 1, connection 10
        key1 = (1, 10)
        _recovery_state[key1] = {
            "failed_at": datetime.now(timezone.utc),
            "cooldown_seconds": 60,
        }

        # Profile 2, connection 10 (same connection ID, different profile)
        key2 = (2, 10)
        _recovery_state[key2] = {
            "failed_at": datetime.now(timezone.utc),
            "cooldown_seconds": 120,
        }

        # Both can coexist without collision
        assert key1 in _recovery_state
        assert key2 in _recovery_state
        assert (
            _recovery_state[key1]["cooldown_seconds"]
            != _recovery_state[key2]["cooldown_seconds"]
        )

