import asyncio
import json
import logging
from typing import AsyncGenerator, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services.proxy_service import (
    extract_model_from_body,
    build_upstream_headers,
)
from app.services.stats_service import log_request


class TestFailoverRecoveryFieldValidation:
    """Validate failover recovery field validation and config version 2."""

    def test_recovery_cooldown_validates_lower_bound(self):
        """Recovery cooldown must be >= 1."""
        from app.schemas.schemas import ModelConfigBase
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            ModelConfigBase(
                provider_id=1,
                model_id="gpt-4",
                display_name="GPT-4",
                model_type="native",
                lb_strategy="failover",
                failover_recovery_cooldown_seconds=0,
            )
        assert "must be between 1 and 3600" in str(exc_info.value)

    def test_recovery_cooldown_validates_upper_bound(self):
        """Recovery cooldown must be <= 3600."""
        from app.schemas.schemas import ModelConfigBase
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            ModelConfigBase(
                provider_id=1,
                model_id="gpt-4",
                display_name="GPT-4",
                model_type="native",
                lb_strategy="failover",
                failover_recovery_cooldown_seconds=3601,
            )
        assert "must be between 1 and 3600" in str(exc_info.value)

    def test_recovery_cooldown_accepts_valid_values(self):
        """Recovery cooldown accepts values in range [1, 3600]."""
        from app.schemas.schemas import ModelConfigBase

        # Lower bound
        config = ModelConfigBase(
            provider_id=1,
            model_id="gpt-4",
            display_name="GPT-4",
            model_type="native",
            lb_strategy="failover",
            failover_recovery_cooldown_seconds=1,
        )
        assert config.failover_recovery_cooldown_seconds == 1

        # Upper bound
        config = ModelConfigBase(
            provider_id=1,
            model_id="gpt-4",
            display_name="GPT-4",
            model_type="native",
            lb_strategy="failover",
            failover_recovery_cooldown_seconds=3600,
        )
        assert config.failover_recovery_cooldown_seconds == 3600

        # Mid-range
        config = ModelConfigBase(
            provider_id=1,
            model_id="gpt-4",
            display_name="GPT-4",
            model_type="native",
            lb_strategy="failover",
            failover_recovery_cooldown_seconds=120,
        )
        assert config.failover_recovery_cooldown_seconds == 120

    def test_lb_strategy_rejects_round_robin(self):
        """lb_strategy field rejects round_robin value."""
        from app.schemas.schemas import ModelConfigBase
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            ModelConfigBase.model_validate(
                {
                    "provider_id": 1,
                    "model_id": "gpt-4",
                    "display_name": "GPT-4",
                    "model_type": "native",
                    "lb_strategy": "round_robin",
                }
            )
        assert "Input should be 'single' or 'failover'" in str(exc_info.value)

    def test_config_export_includes_recovery_fields(self):
        """ConfigModelExport includes recovery fields."""
        from app.schemas.schemas import ConfigConnectionExport, ConfigModelExport

        model = ConfigModelExport(
            provider_type="openai",
            model_id="gpt-4",
            display_name="GPT-4",
            model_type="native",
            lb_strategy="failover",
            is_enabled=True,
            failover_recovery_enabled=False,
            failover_recovery_cooldown_seconds=300,
            connections=[
                ConfigConnectionExport(
                    endpoint_name="openai-main",
                )
            ],
        )
        exported = model.model_dump(mode="json")
        assert exported["failover_recovery_enabled"] is False
        assert exported["failover_recovery_cooldown_seconds"] == 300

    def test_config_import_accepts_minimal_payload(self):
        """ConfigImportRequest accepts minimal strict payload."""
        from app.schemas.schemas import ConfigImportRequest

        validation = ConfigImportRequest.model_validate(
            {
                "version": 2,
                "endpoints": [],
                "pricing_templates": [],
                "models": [],
            }
        )
        assert validation.endpoints == []
        assert validation.models == []

    def test_config_import_rejects_round_robin_in_models(self):
        """ConfigImportRequest rejects models with lb_strategy=round_robin."""
        from app.schemas.schemas import ConfigImportRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            ConfigImportRequest.model_validate(
                {
                    "version": 2,
                    "endpoints": [
                        {
                            "endpoint_id": 1,
                            "name": "openai-main",
                            "base_url": "https://api.openai.com/v1",
                            "api_key": "sk-test",
                            "position": 0,
                        }
                    ],
                    "pricing_templates": [],
                    "models": [
                        {
                            "provider_type": "openai",
                            "model_id": "gpt-4",
                            "display_name": "GPT-4",
                            "model_type": "native",
                            "lb_strategy": "round_robin",
                            "is_enabled": True,
                            "connections": [
                                {
                                    "connection_id": 1,
                                    "endpoint_id": 1,
                                }
                            ],
                        }
                    ],
                }
            )
        assert "Input should be 'single' or 'failover'" in str(exc_info.value)

    def test_config_import_accepts_duplicate_connection_id(self):
        """Config import validation ignores duplicate connection IDs."""
        from app.routers.config import _validate_import
        from app.schemas.schemas import ConfigImportRequest

        data = ConfigImportRequest.model_validate(
            {
                "version": 2,
                "endpoints": [
                    {
                        "endpoint_id": 1,
                        "name": "openai-main",
                        "base_url": "https://api.openai.com/v1",
                        "api_key": "sk-test",
                        "position": 0,
                    }
                ],
                "pricing_templates": [],
                "models": [
                    {
                        "provider_type": "openai",
                        "model_id": "gpt-4o",
                        "model_type": "native",
                        "connections": [
                            {
                                "connection_id": 1,
                                "endpoint_id": 1,
                            }
                        ],
                    },
                    {
                        "provider_type": "openai",
                        "model_id": "gpt-4.1",
                        "model_type": "native",
                        "connections": [
                            {
                                "connection_id": 1,
                                "endpoint_id": 1,
                            }
                        ],
                    },
                ],
            }
        )

        _validate_import(data)

    def test_config_roundtrip(self):
        """Config export/import roundtrip with strict schema and recovery fields."""
        from app.schemas.schemas import (
            ConfigConnectionExport,
            ConfigEndpointExport,
            ConfigExportResponse,
            ConfigImportRequest,
            ConfigModelExport,
        )
        from datetime import datetime, timezone

        config = ConfigExportResponse(
            exported_at=datetime.now(timezone.utc),
            endpoints=[
                ConfigEndpointExport(
                    name="openai-main",
                    base_url="https://api.openai.com/v1",
                    api_key="sk-test",
                    position=0,
                )
            ],
            pricing_templates=[],
            models=[
                ConfigModelExport(
                    provider_type="openai",
                    model_id="gpt-4o",
                    display_name="GPT-4o",
                    model_type="native",
                    lb_strategy="failover",
                    is_enabled=True,
                    failover_recovery_enabled=False,
                    failover_recovery_cooldown_seconds=180,
                    connections=[
                        ConfigConnectionExport(
                            endpoint_name="openai-main",
                            is_active=True,
                            priority=0,
                        )
                    ],
                )
            ],
        )
        exported = config.model_dump(mode="json")
        reimported = ConfigImportRequest(**exported)

        assert len(reimported.models) == 1
        m = reimported.models[0]
        assert m.lb_strategy == "failover"
        assert m.failover_recovery_enabled is False
        assert m.failover_recovery_cooldown_seconds == 180


class TestDEF010_EndpointToggleClearsRecoveryState:
    def _make_connection(self, connection_id: int):
        from app.models.models import Connection

        return Connection(
            id=connection_id,
            model_config_id=1,
            endpoint_id=99,
            is_active=True,
            priority=0,
        )

    @pytest.mark.asyncio
    async def test_update_endpoint_disable_clears_recovery_state(self):
        from app.routers.connections import update_connection
        from app.schemas.schemas import ConnectionUpdate

        connection = self._make_connection(401)

        mock_db = AsyncMock()
        mock_db.get = AsyncMock(return_value=connection)
        mock_db.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(return_value=connection)
            )
        )
        mock_db.flush = AsyncMock()

        with patch(
            "app.routers.connections.clear_connection_state", AsyncMock()
        ) as clear_state:
            await update_connection(
                connection_id=connection.id,
                body=ConnectionUpdate(is_active=False),
                db=mock_db,
                profile_id=1,
            )

        assert connection.is_active is False
        clear_state.assert_awaited_once_with(1, connection.id)

    @pytest.mark.asyncio
    async def test_delete_endpoint_clears_recovery_state(self):
        from app.routers.connections import delete_connection

        connection = self._make_connection(402)

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(return_value=connection)
            )
        )
        mock_db.delete = AsyncMock()

        with patch(
            "app.routers.connections.clear_connection_state", AsyncMock()
        ) as clear_state:
            await delete_connection(
                connection_id=connection.id, db=mock_db, profile_id=1
            )

        clear_state.assert_awaited_once_with(1, connection.id)
        mock_db.delete.assert_awaited_once_with(connection)


class TestLoadbalanceCurrentStateContracts:
    @pytest.mark.asyncio
    async def test_current_state_list_returns_derived_states(self):
        from datetime import datetime, timezone

        from app.routers.loadbalance import list_loadbalance_current_state
        from app.schemas.schemas import (
            LoadbalanceCurrentStateItem,
            LoadbalanceCurrentStateListResponse,
        )

        blocked_until = datetime(2099, 1, 1, tzinfo=timezone.utc)
        probe_eligible = datetime(2020, 1, 1, tzinfo=timezone.utc)
        created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        updated_at = datetime(2025, 1, 2, tzinfo=timezone.utc)

        mock_db = AsyncMock()
        current_state_response = LoadbalanceCurrentStateListResponse(
            items=[
                LoadbalanceCurrentStateItem(
                    connection_id=1,
                    consecutive_failures=1,
                    last_failure_kind="timeout",
                    last_cooldown_seconds=0,
                    blocked_until_at=None,
                    probe_eligible_logged=False,
                    state="counting",
                    created_at=created_at,
                    updated_at=updated_at,
                ),
                LoadbalanceCurrentStateItem(
                    connection_id=2,
                    consecutive_failures=3,
                    last_failure_kind="transient_http",
                    last_cooldown_seconds=30,
                    blocked_until_at=blocked_until,
                    probe_eligible_logged=False,
                    state="blocked",
                    created_at=created_at,
                    updated_at=updated_at,
                ),
                LoadbalanceCurrentStateItem(
                    connection_id=3,
                    consecutive_failures=4,
                    last_failure_kind="timeout",
                    last_cooldown_seconds=45,
                    blocked_until_at=probe_eligible,
                    probe_eligible_logged=True,
                    state="probe_eligible",
                    created_at=created_at,
                    updated_at=updated_at,
                ),
            ]
        )

        with patch(
            "app.routers.loadbalance.list_model_current_state",
            AsyncMock(return_value=current_state_response),
        ) as list_model_current_state:
            response = await list_loadbalance_current_state(
                db=mock_db,
                profile_id=5,
                model_config_id=11,
            )

        list_model_current_state.assert_awaited_once_with(
            db=mock_db,
            profile_id=5,
            model_config_id=11,
        )
        assert [item.state for item in response.items] == [
            "counting",
            "blocked",
            "probe_eligible",
        ]
        assert [item.connection_id for item in response.items] == [1, 2, 3]
        assert response.items[0].probe_eligible_logged is False
        assert response.items[1].last_failure_kind == "transient_http"
        assert response.items[2].probe_eligible_logged is True

    @pytest.mark.asyncio
    async def test_current_state_list_returns_404_for_model_outside_profile(self):
        from fastapi import HTTPException

        from app.routers.loadbalance import list_loadbalance_current_state

        mock_db = AsyncMock()
        mock_db.scalar = AsyncMock(return_value=None)

        with pytest.raises(HTTPException) as exc_info:
            await list_loadbalance_current_state(
                db=mock_db,
                profile_id=5,
                model_config_id=11,
            )

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "Model not found"

    @pytest.mark.asyncio
    async def test_current_state_reset_is_idempotent(self):
        from app.routers.loadbalance import reset_loadbalance_current_state
        from app.schemas.schemas import LoadbalanceCurrentStateResetResponse

        reset_response = LoadbalanceCurrentStateResetResponse(
            connection_id=44,
            cleared=False,
        )

        with patch(
            "app.routers.loadbalance.reset_connection_current_state",
            AsyncMock(return_value=reset_response),
        ) as reset_connection_current_state:
            response = await reset_loadbalance_current_state(
                connection_id=44,
                profile_id=8,
            )

        reset_connection_current_state.assert_awaited_once_with(
            profile_id=8,
            connection_id=44,
        )
        assert response.connection_id == 44
        assert response.cleared is False
