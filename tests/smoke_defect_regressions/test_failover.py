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
                    connection_id=1,
                    endpoint_id=1,
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

    def test_config_import_rejects_duplicate_connection_id(self):
        """Config import validation rejects duplicate connection IDs."""
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

        with pytest.raises(HTTPException) as exc_info:
            _validate_import(data)

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Duplicate connection_id in import: 1"

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
                    endpoint_id=1,
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
                            connection_id=1,
                            endpoint_id=1,
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
        from app.services.loadbalancer import _recovery_state, mark_connection_failed

        connection = self._make_connection(401)
        mark_connection_failed(1, connection.id, 60, 10.0, "transient_http")

        mock_db = AsyncMock()
        mock_db.get = AsyncMock(return_value=connection)
        mock_db.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(return_value=connection)
            )
        )
        mock_db.flush = AsyncMock()

        try:
            await update_connection(
                connection_id=connection.id,
                body=ConnectionUpdate(is_active=False),
                db=mock_db,
                profile_id=1,
            )
            assert connection.is_active is False
            assert (1, connection.id) not in _recovery_state
        finally:
            _recovery_state.pop((1, connection.id), None)

    @pytest.mark.asyncio
    async def test_delete_endpoint_clears_recovery_state(self):
        from app.routers.connections import delete_connection
        from app.services.loadbalancer import _recovery_state, mark_connection_failed

        connection = self._make_connection(402)
        mark_connection_failed(1, connection.id, 60, 10.0, "transient_http")

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=connection)))
        mock_db.delete = AsyncMock()

        try:
            await delete_connection(connection_id=connection.id, db=mock_db, profile_id=1)
            assert (1, connection.id) not in _recovery_state
            mock_db.delete.assert_awaited_once_with(connection)
        finally:
            _recovery_state.pop((1, connection.id), None)

