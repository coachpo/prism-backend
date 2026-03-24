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

from tests.loadbalance_strategy_helpers import make_loadbalance_strategy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException
from sqlalchemy import select, func
from datetime import datetime, timezone
from uuid import uuid4

from app.core.database import AsyncSessionLocal
from app.models.models import (
    Profile,
    Provider,
    ModelConfig,
    Endpoint,
    Connection,
    LoadbalanceCurrentState,
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

        assert resolved is not None
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
    @pytest.mark.asyncio
    async def test_current_state_reads_are_profile_scoped(self):
        from app.services.loadbalancer.state import (
            get_current_states_for_connections,
        )

        suffix = uuid4().hex[:8]

        async with AsyncSessionLocal() as session:
            provider = Provider(
                name=f"OpenAI Isolation {suffix}",
                provider_type="openai",
                audit_enabled=False,
                audit_capture_bodies=False,
            )
            profile_one = Profile(
                name=f"Isolation One {suffix}", is_active=False, version=0
            )
            profile_two = Profile(
                name=f"Isolation Two {suffix}", is_active=False, version=0
            )
            model_one = ModelConfig(
                provider=provider,
                profile=profile_one,
                model_id=f"iso-model-one-{suffix}",
                display_name="Isolation Model One",
                model_type="native",
                loadbalance_strategy=make_loadbalance_strategy(
                    profile=profile_one,
                    strategy_type="failover",
                ),
                is_enabled=True,
            )
            model_two = ModelConfig(
                provider=provider,
                profile=profile_two,
                model_id=f"iso-model-two-{suffix}",
                display_name="Isolation Model Two",
                model_type="native",
                loadbalance_strategy=make_loadbalance_strategy(
                    profile=profile_two,
                    strategy_type="failover",
                ),
                is_enabled=True,
            )
            endpoint_one = Endpoint(
                profile=profile_one,
                name=f"endpoint-one-{suffix}",
                base_url="https://one.example.com/v1",
                api_key="sk-one",
                position=0,
            )
            endpoint_two = Endpoint(
                profile=profile_two,
                name=f"endpoint-two-{suffix}",
                base_url="https://two.example.com/v1",
                api_key="sk-two",
                position=0,
            )
            connection_one = Connection(
                profile=profile_one,
                model_config_rel=model_one,
                endpoint_rel=endpoint_one,
                is_active=True,
                priority=0,
                name="one",
            )
            connection_two = Connection(
                profile=profile_two,
                model_config_rel=model_two,
                endpoint_rel=endpoint_two,
                is_active=True,
                priority=0,
                name="two",
            )

            session.add_all(
                [
                    provider,
                    profile_one,
                    profile_two,
                    model_one,
                    model_two,
                    endpoint_one,
                    endpoint_two,
                    connection_one,
                    connection_two,
                ]
            )
            await session.commit()
            await session.refresh(connection_one)
            await session.refresh(connection_two)

            session.add_all(
                [
                    LoadbalanceCurrentState(
                        profile_id=profile_one.id,
                        connection_id=connection_one.id,
                        consecutive_failures=2,
                        last_failure_kind="transient_http",
                        last_cooldown_seconds=60.0,
                        probe_eligible_logged=False,
                    ),
                    LoadbalanceCurrentState(
                        profile_id=profile_two.id,
                        connection_id=connection_two.id,
                        consecutive_failures=4,
                        last_failure_kind="timeout",
                        last_cooldown_seconds=120.0,
                        probe_eligible_logged=True,
                    ),
                ]
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            rows = await get_current_states_for_connections(
                session,
                profile_id=profile_one.id,
                connection_ids=[connection_one.id, connection_two.id],
            )

        assert set(rows) == {connection_one.id}
        assert connection_two.id not in rows
        assert float(rows[connection_one.id].last_cooldown_seconds) == 60.0
        assert rows[connection_one.id].last_failure_kind == "transient_http"
