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


class TestLoadbalanceStrategyFieldValidation:
    def test_native_model_requires_strategy_id(self):
        from app.schemas.schemas import ModelConfigBase
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            ModelConfigBase(
                vendor_id=1,
                api_family="openai",
                model_id="gpt-4",
                display_name="GPT-4",
                model_type="native",
            )
        assert "loadbalance_strategy_id is required for native models" in str(
            exc_info.value
        )

    def test_proxy_model_rejects_strategy_id(self):
        from app.schemas.schemas import ModelConfigBase, ProxyTargetReference
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            ModelConfigBase(
                vendor_id=1,
                api_family="openai",
                model_id="gpt-4-proxy",
                model_type="proxy",
                proxy_targets=[
                    ProxyTargetReference(target_model_id="gpt-4", position=0)
                ],
                loadbalance_strategy_id=7,
            )
        assert "loadbalance_strategy_id must be null for proxy models" in str(
            exc_info.value
        )

    def test_single_strategy_rejects_recovery_enabled(self):
        from app.schemas.schemas import LoadbalanceStrategyCreate
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            LoadbalanceStrategyCreate(
                name="single-with-recovery",
                strategy_type="single",
                failover_recovery_enabled=True,
            )
        assert "single strategies must not enable failover recovery" in str(
            exc_info.value
        )

    def test_config_export_includes_strategy_reference(self):
        from app.schemas.schemas import (
            ConfigConnectionExport,
            ConfigLoadbalanceStrategyExport,
            ConfigModelExport,
        )

        strategy = ConfigLoadbalanceStrategyExport(
            name="failover-primary",
            strategy_type="failover",
            failover_recovery_enabled=False,
        )
        model = ConfigModelExport(
            vendor_key="openai",
            api_family="openai",
            model_id="gpt-4",
            display_name="GPT-4",
            model_type="native",
            loadbalance_strategy_name="failover-primary",
            is_enabled=True,
            connections=[
                ConfigConnectionExport(
                    endpoint_name="openai-main",
                )
            ],
        )
        assert strategy.failover_recovery_enabled is False
        exported = model.model_dump(mode="json")
        assert exported["loadbalance_strategy_name"] == "failover-primary"

    def test_config_export_includes_explicit_failover_policy_fields(self):
        from app.schemas.schemas import ConfigLoadbalanceStrategyExport

        strategy = ConfigLoadbalanceStrategyExport(
            name="failover-primary",
            strategy_type="failover",
            failover_recovery_enabled=True,
            failover_cooldown_seconds=45,
            failover_failure_threshold=4,
            failover_backoff_multiplier=3.5,
            failover_max_cooldown_seconds=720,
            failover_jitter_ratio=0.35,
            failover_status_codes=[503, 429],
            failover_ban_mode="temporary",
            failover_max_cooldown_strikes_before_ban=3,
            failover_ban_duration_seconds=600,
        )

        exported = strategy.model_dump(mode="json")

        assert exported["failover_cooldown_seconds"] == 45
        assert exported["failover_failure_threshold"] == 4
        assert exported["failover_backoff_multiplier"] == 3.5
        assert exported["failover_max_cooldown_seconds"] == 720
        assert exported["failover_jitter_ratio"] == 0.35
        assert exported["failover_status_codes"] == [429, 503]
        assert exported["failover_ban_mode"] == "temporary"
        assert exported["failover_max_cooldown_strikes_before_ban"] == 3
        assert exported["failover_ban_duration_seconds"] == 600

    def test_strategy_contract_accepts_sorted_unique_failover_status_codes(self):
        from app.schemas.schemas import LoadbalanceStrategyCreate

        strategy = LoadbalanceStrategyCreate(
            name="failover-primary",
            strategy_type="failover",
            failover_recovery_enabled=True,
            failover_status_codes=[503, 429],
        )

        assert strategy.failover_status_codes == [429, 503]

    def test_strategy_contract_rejects_out_of_range_failover_status_codes(self):
        from app.schemas.schemas import LoadbalanceStrategyCreate
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            LoadbalanceStrategyCreate(
                name="failover-primary",
                strategy_type="failover",
                failover_recovery_enabled=True,
                failover_status_codes=[99, 503],
            )

        assert "failover_status_codes" in str(exc_info.value)

    def test_strategy_contract_rejects_legacy_auth_cooldown_field(self):
        from app.schemas.schemas import LoadbalanceStrategyCreate
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            LoadbalanceStrategyCreate.model_validate(
                {
                    "name": "failover-primary",
                    "strategy_type": "failover",
                    "failover_recovery_enabled": True,
                    "failover_status_codes": [429, 503],
                    "failover_auth_error_cooldown_seconds": 2400,
                }
            )

        assert "failover_auth_error_cooldown_seconds" in str(exc_info.value)

    def test_config_export_version_7_allows_fill_first_strategy(self):
        from datetime import datetime, timezone

        from app.schemas.schemas import (
            ConfigExportResponse,
            ConfigLoadbalanceStrategyExport,
            ConfigVendorExport,
        )

        config = ConfigExportResponse(
            exported_at=datetime.now(timezone.utc),
            vendors=[
                ConfigVendorExport(
                    key="openai",
                    name="OpenAI",
                    description=None,
                    audit_enabled=False,
                    audit_capture_bodies=True,
                )
            ],
            endpoints=[],
            pricing_templates=[],
            loadbalance_strategies=[
                ConfigLoadbalanceStrategyExport(
                    name="fill-first-primary",
                    strategy_type="fill-first",
                    failover_recovery_enabled=True,
                    failover_cooldown_seconds=45,
                    failover_failure_threshold=4,
                    failover_backoff_multiplier=3.5,
                    failover_max_cooldown_seconds=720,
                    failover_jitter_ratio=0.35,
                    failover_status_codes=[503, 429],
                    failover_ban_mode="temporary",
                    failover_max_cooldown_strikes_before_ban=3,
                    failover_ban_duration_seconds=600,
                )
            ],
            models=[],
        )

        exported = config.model_dump(mode="json")

        assert exported["version"] == 7
        assert exported["loadbalance_strategies"][0]["strategy_type"] == "fill-first"
        assert (
            exported["loadbalance_strategies"][0]["failover_recovery_enabled"] is True
        )

    def test_config_export_version_7_allows_round_robin_strategy(self):
        from datetime import datetime, timezone

        from app.schemas.schemas import (
            ConfigExportResponse,
            ConfigLoadbalanceStrategyExport,
            ConfigVendorExport,
        )

        config = ConfigExportResponse(
            exported_at=datetime.now(timezone.utc),
            vendors=[
                ConfigVendorExport(
                    key="openai",
                    name="OpenAI",
                    description=None,
                    audit_enabled=False,
                    audit_capture_bodies=True,
                )
            ],
            endpoints=[],
            pricing_templates=[],
            loadbalance_strategies=[
                ConfigLoadbalanceStrategyExport(
                    name="round-robin-primary",
                    strategy_type="round-robin",
                    failover_recovery_enabled=True,
                    failover_cooldown_seconds=45,
                    failover_failure_threshold=4,
                    failover_backoff_multiplier=3.5,
                    failover_max_cooldown_seconds=720,
                    failover_jitter_ratio=0.35,
                    failover_status_codes=[503, 429],
                    failover_ban_mode="temporary",
                    failover_max_cooldown_strikes_before_ban=3,
                    failover_ban_duration_seconds=600,
                )
            ],
            models=[],
        )

        exported = config.model_dump(mode="json")

        assert exported["version"] == 7
        assert exported["loadbalance_strategies"][0]["strategy_type"] == "round-robin"
        assert (
            exported["loadbalance_strategies"][0]["failover_recovery_enabled"] is True
        )

    def test_config_import_accepts_minimal_payload(self):
        from app.schemas.schemas import ConfigImportRequest

        validation = ConfigImportRequest.model_validate(
            {
                "version": 7,
                "vendors": [],
                "endpoints": [],
                "pricing_templates": [],
                "loadbalance_strategies": [],
                "models": [],
            }
        )
        assert validation.endpoints == []
        assert validation.loadbalance_strategies == []
        assert validation.models == []

    def test_config_import_requires_explicit_failover_status_codes_under_version_7(
        self,
    ):
        from app.schemas.schemas import ConfigImportRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ConfigImportRequest.model_validate(
                {
                    "version": 7,
                    "vendors": [
                        {
                            "key": "openai",
                            "name": "OpenAI",
                            "description": None,
                            "audit_enabled": False,
                            "audit_capture_bodies": True,
                        }
                    ],
                    "endpoints": [
                        {
                            "name": "openai-main",
                            "base_url": "https://api.openai.com",
                            "api_key": "sk-test",
                        }
                    ],
                    "pricing_templates": [],
                    "loadbalance_strategies": [
                        {
                            "name": "failover-primary",
                            "strategy_type": "failover",
                            "failover_recovery_enabled": True,
                        }
                    ],
                    "models": [
                        {
                            "vendor_key": "openai",
                            "api_family": "openai",
                            "model_id": "gpt-4o",
                            "model_type": "native",
                            "loadbalance_strategy_name": "failover-primary",
                            "connections": [{"endpoint_name": "openai-main"}],
                        }
                    ],
                }
            )

    def test_config_import_rejects_native_model_missing_strategy_name(self):
        from app.routers.config import _validate_import
        from app.schemas.schemas import ConfigImportRequest

        data = ConfigImportRequest.model_validate(
            {
                "version": 7,
                "vendors": [
                    {
                        "key": "openai",
                        "name": "OpenAI",
                        "description": None,
                        "audit_enabled": False,
                        "audit_capture_bodies": True,
                    }
                ],
                "endpoints": [
                    {
                        "name": "openai-main",
                        "base_url": "https://api.openai.com",
                        "api_key": "sk-test",
                        "position": 0,
                    }
                ],
                "pricing_templates": [],
                "loadbalance_strategies": [],
                "models": [
                    {
                        "vendor_key": "openai",
                        "api_family": "openai",
                        "model_id": "gpt-4",
                        "display_name": "GPT-4",
                        "model_type": "native",
                        "is_enabled": True,
                        "connections": [
                            {
                                "endpoint_name": "openai-main",
                            }
                        ],
                    }
                ],
            }
        )

        with pytest.raises(HTTPException) as exc_info:
            _validate_import(data)
        assert exc_info.value.detail == (
            "Native model 'gpt-4' must include loadbalance_strategy_name"
        )

    def test_config_import_accepts_duplicate_connection_endpoints(self):
        from app.routers.config import _validate_import
        from app.schemas.schemas import ConfigImportRequest

        data = ConfigImportRequest.model_validate(
            {
                "version": 7,
                "vendors": [
                    {
                        "key": "openai",
                        "name": "OpenAI",
                        "description": None,
                        "audit_enabled": False,
                        "audit_capture_bodies": True,
                    }
                ],
                "endpoints": [
                    {
                        "name": "openai-main",
                        "base_url": "https://api.openai.com",
                        "api_key": "sk-test",
                        "position": 0,
                    }
                ],
                "pricing_templates": [],
                "loadbalance_strategies": [
                    {
                        "name": "single-primary",
                        "strategy_type": "single",
                        "failover_recovery_enabled": False,
                        "failover_status_codes": [429, 503],
                    }
                ],
                "models": [
                    {
                        "vendor_key": "openai",
                        "api_family": "openai",
                        "model_id": "gpt-4o",
                        "model_type": "native",
                        "loadbalance_strategy_name": "single-primary",
                        "connections": [
                            {
                                "endpoint_name": "openai-main",
                            }
                        ],
                    },
                    {
                        "vendor_key": "openai",
                        "api_family": "openai",
                        "model_id": "gpt-4.1",
                        "model_type": "native",
                        "loadbalance_strategy_name": "single-primary",
                        "connections": [
                            {
                                "endpoint_name": "openai-main",
                            }
                        ],
                    },
                ],
            }
        )

        _validate_import(data)

    def test_config_roundtrip(self):
        from app.schemas.schemas import (
            ConfigConnectionExport,
            ConfigEndpointExport,
            ConfigExportResponse,
            ConfigImportRequest,
            ConfigLoadbalanceStrategyExport,
            ConfigModelExport,
            ConfigVendorExport,
        )
        from datetime import datetime, timezone

        config = ConfigExportResponse(
            exported_at=datetime.now(timezone.utc),
            vendors=[
                ConfigVendorExport(
                    key="openai",
                    name="OpenAI",
                    description=None,
                    audit_enabled=False,
                    audit_capture_bodies=True,
                )
            ],
            endpoints=[
                ConfigEndpointExport(
                    name="openai-main",
                    base_url="https://api.openai.com",
                    api_key="sk-test",
                    position=0,
                )
            ],
            pricing_templates=[],
            loadbalance_strategies=[
                ConfigLoadbalanceStrategyExport(
                    name="failover-primary",
                    strategy_type="failover",
                    failover_recovery_enabled=False,
                    failover_cooldown_seconds=45,
                    failover_failure_threshold=4,
                    failover_backoff_multiplier=3.5,
                    failover_max_cooldown_seconds=720,
                    failover_jitter_ratio=0.35,
                    failover_status_codes=[503, 429],
                    failover_ban_mode="off",
                    failover_max_cooldown_strikes_before_ban=0,
                    failover_ban_duration_seconds=0,
                )
            ],
            models=[
                ConfigModelExport(
                    vendor_key="openai",
                    api_family="openai",
                    model_id="gpt-4o",
                    display_name="GPT-4o",
                    model_type="native",
                    loadbalance_strategy_name="failover-primary",
                    is_enabled=True,
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

        assert len(reimported.loadbalance_strategies) == 1
        assert len(reimported.models) == 1
        strategy = reimported.loadbalance_strategies[0]
        assert strategy.name == "failover-primary"
        assert strategy.failover_recovery_enabled is False
        assert strategy.failover_cooldown_seconds == 45
        assert strategy.failover_failure_threshold == 4
        assert strategy.failover_backoff_multiplier == 3.5
        assert strategy.failover_max_cooldown_seconds == 720
        assert strategy.failover_jitter_ratio == 0.35
        assert strategy.failover_status_codes == [429, 503]
        assert strategy.failover_ban_mode == "off"
        assert strategy.failover_max_cooldown_strikes_before_ban == 0
        assert strategy.failover_ban_duration_seconds == 0
        model = reimported.models[0]
        assert model.loadbalance_strategy_name == "failover-primary"


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
                    max_cooldown_strikes=0,
                    ban_mode="off",
                    banned_until_at=None,
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
                    max_cooldown_strikes=1,
                    ban_mode="off",
                    banned_until_at=None,
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
                    max_cooldown_strikes=1,
                    ban_mode="off",
                    banned_until_at=None,
                    blocked_until_at=probe_eligible,
                    probe_eligible_logged=True,
                    state="probe_eligible",
                    created_at=created_at,
                    updated_at=updated_at,
                ),
                LoadbalanceCurrentStateItem(
                    connection_id=4,
                    consecutive_failures=5,
                    last_failure_kind="transient_http",
                    last_cooldown_seconds=60,
                    max_cooldown_strikes=2,
                    ban_mode="temporary",
                    banned_until_at=blocked_until,
                    blocked_until_at=blocked_until,
                    probe_eligible_logged=False,
                    state="banned",
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
            "banned",
        ]
        assert [item.connection_id for item in response.items] == [1, 2, 3, 4]
        assert response.items[0].probe_eligible_logged is False
        assert response.items[1].last_failure_kind == "transient_http"
        assert response.items[2].probe_eligible_logged is True
        assert response.items[3].max_cooldown_strikes == 2
        assert response.items[3].ban_mode == "temporary"

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
