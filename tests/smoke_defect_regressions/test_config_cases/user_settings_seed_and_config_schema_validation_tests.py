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


class TestDEF031_StartupUserSettingsSeed:
    """DEF-031 (P0): startup must seed user settings with profile_id."""

    @pytest.mark.asyncio
    async def test_seed_user_settings_creates_missing_rows_per_profile(self):
        from app.main import seed_user_settings
        from app.models.models import UserSetting

        profile_ids_result = MagicMock()
        profile_ids_scalars = MagicMock()
        profile_ids_scalars.all.return_value = [1, 2]
        profile_ids_result.scalars.return_value = profile_ids_scalars

        existing_profile_ids_result = MagicMock()
        existing_ids_scalars = MagicMock()
        existing_ids_scalars.all.return_value = [2]
        existing_profile_ids_result.scalars.return_value = existing_ids_scalars

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock(
            side_effect=[profile_ids_result, existing_profile_ids_result]
        )

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "app.core.database.AsyncSessionLocal",
            return_value=mock_session_ctx,
        ):
            await seed_user_settings()

        mock_session.add.assert_called_once()
        seeded_setting = mock_session.add.call_args.args[0]
        assert isinstance(seeded_setting, UserSetting)
        assert seeded_setting.profile_id == 1
        assert seeded_setting.report_currency_code == "USD"
        assert seeded_setting.report_currency_symbol == "$"
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_seed_user_settings_noops_when_no_profiles_exist(self):
        from app.main import seed_user_settings

        profile_ids_result = MagicMock()
        profile_ids_scalars = MagicMock()
        profile_ids_scalars.all.return_value = []
        profile_ids_result.scalars.return_value = profile_ids_scalars

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock(return_value=profile_ids_result)

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "app.core.database.AsyncSessionLocal",
            return_value=mock_session_ctx,
        ):
            await seed_user_settings()

        mock_session.add.assert_not_called()
        mock_session.commit.assert_not_awaited()


class TestDEF006_ConfigExportImportFieldCoverage:
    """DEF-006 (P0): config export/import must preserve all mutable fields including custom_headers."""

    def test_export_schema_includes_all_endpoint_fields(self):
        from app.schemas.schemas import ConfigEndpointExport

        fields = set(ConfigEndpointExport.model_fields.keys())
        expected = {
            "name",
            "base_url",
            "api_key",
            "position",
        }
        assert expected.issubset(fields), f"Missing fields: {expected - fields}"

    def test_export_schema_includes_all_connection_fields(self):
        from app.schemas.schemas import ConfigConnectionExport

        fields = set(ConfigConnectionExport.model_fields.keys())
        expected = {
            "endpoint_name",
            "pricing_template_name",
            "is_active",
            "priority",
            "name",
            "auth_type",
            "custom_headers",
            "qps_limit",
            "max_in_flight_non_stream",
            "max_in_flight_stream",
        }
        assert expected.issubset(fields), f"Missing fields: {expected - fields}"

    def test_connection_response_schema_includes_limiter_fields(self):
        from app.schemas.schemas import ConnectionResponse

        fields = set(ConnectionResponse.model_fields.keys())
        expected = {
            "qps_limit",
            "max_in_flight_non_stream",
            "max_in_flight_stream",
        }
        assert expected.issubset(fields), f"Missing fields: {expected - fields}"

    def test_config_export_schema_defaults_to_version_5(self):
        from app.schemas.schemas import ConfigExportResponse

        assert ConfigExportResponse.model_fields["version"].default == 5

    def test_export_schema_includes_all_model_fields(self):
        from app.schemas.schemas import ConfigModelExport

        fields = set(ConfigModelExport.model_fields.keys())
        expected = {
            "provider_type",
            "model_id",
            "display_name",
            "model_type",
            "proxy_targets",
            "loadbalance_strategy_name",
            "is_enabled",
            "connections",
        }
        assert expected.issubset(fields), f"Missing fields: {expected - fields}"
        assert "redirect_to" not in fields

    def test_request_log_response_schema_includes_resolved_target_model_id(self):
        from app.schemas.schemas import RequestLogResponse

        fields = set(RequestLogResponse.model_fields.keys())
        assert "resolved_target_model_id" in fields

    def test_export_schema_includes_all_loadbalance_strategy_fields(self):
        from app.schemas.schemas import ConfigLoadbalanceStrategyExport

        fields = set(ConfigLoadbalanceStrategyExport.model_fields.keys())
        expected = {
            "name",
            "strategy_type",
            "failover_recovery_enabled",
            "failover_cooldown_seconds",
            "failover_failure_threshold",
            "failover_backoff_multiplier",
            "failover_max_cooldown_seconds",
            "failover_jitter_ratio",
            "failover_auth_error_cooldown_seconds",
        }
        assert expected.issubset(fields), f"Missing fields: {expected - fields}"

    def test_roundtrip_custom_headers_preserved(self):
        from app.schemas.schemas import ConfigConnectionExport

        headers = {"X-Custom": "value", "X-Another": "test"}
        connection = ConfigConnectionExport(
            endpoint_name="openai-main",
            custom_headers=headers,
            auth_type="openai",
        )
        exported = connection.model_dump(mode="json")
        reimported = ConfigConnectionExport(**exported)
        assert reimported.custom_headers == headers
        assert reimported.auth_type == "openai"

    def test_roundtrip_custom_headers_null(self):
        from app.schemas.schemas import ConfigConnectionExport

        connection = ConfigConnectionExport(
            endpoint_name="openai-main",
            custom_headers=None,
        )
        exported = connection.model_dump(mode="json")
        reimported = ConfigConnectionExport(**exported)
        assert reimported.custom_headers is None

    def test_roundtrip_custom_headers_empty_dict(self):
        from app.schemas.schemas import ConfigConnectionExport

        connection = ConfigConnectionExport(
            endpoint_name="openai-main",
            custom_headers={},
        )
        exported = connection.model_dump(mode="json")
        reimported = ConfigConnectionExport(**exported)
        assert reimported.custom_headers == {}

    def test_import_serializes_custom_headers_to_json_string(self):
        import json

        headers = {"X-Custom": "value"}
        serialized = json.dumps(headers) if headers is not None else None
        assert serialized == '{"X-Custom": "value"}'
        assert json.loads(serialized) == headers

    def test_full_config_roundtrip_schema(self):
        from app.schemas.schemas import (
            ConfigConnectionExport,
            ConfigEndpointExport,
            ConfigExportResponse,
            ConfigImportRequest,
            ConfigLoadbalanceStrategyExport,
            ConfigModelExport,
        )
        from datetime import datetime, timezone

        config = ConfigExportResponse(
            exported_at=datetime.now(timezone.utc),
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
                    failover_recovery_enabled=True,
                    failover_cooldown_seconds=45,
                    failover_failure_threshold=4,
                    failover_backoff_multiplier=3.5,
                    failover_max_cooldown_seconds=720,
                    failover_jitter_ratio=0.35,
                    failover_auth_error_cooldown_seconds=2400,
                )
            ],
            models=[
                ConfigModelExport(
                    provider_type="openai",
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
                            name="Primary",
                            auth_type="openai",
                            custom_headers={"X-Org": "my-org"},
                        )
                    ],
                )
            ],
        )
        exported = config.model_dump(mode="json")
        reimported = ConfigImportRequest(**exported)

        assert len(reimported.models) == 1
        m = reimported.models[0]
        assert m.model_id == "gpt-4o"
        assert m.loadbalance_strategy_name == "failover-primary"
        strategy = reimported.loadbalance_strategies[0]
        assert strategy.failover_cooldown_seconds == 45
        assert strategy.failover_failure_threshold == 4
        assert strategy.failover_backoff_multiplier == 3.5
        assert strategy.failover_max_cooldown_seconds == 720
        assert strategy.failover_jitter_ratio == 0.35
        assert strategy.failover_auth_error_cooldown_seconds == 2400
        assert len(m.connections) == 1
        connection = m.connections[0]
        assert connection.custom_headers == {"X-Org": "my-org"}
        assert connection.auth_type == "openai"
        assert connection.priority == 0
        assert connection.endpoint_name == "openai-main"


class TestDEF023_ConfigImportReferenceValidation:
    def test_validate_import_rejects_legacy_numeric_ids(self):
        from app.schemas.schemas import ConfigImportRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            ConfigImportRequest.model_validate(
                {
                    "version": 3,
                    "endpoints": [
                        {
                            "endpoint_id": 1,
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
                        }
                    ],
                    "models": [
                        {
                            "provider_type": "openai",
                            "model_id": "gpt-4o",
                            "model_type": "native",
                            "loadbalance_strategy_name": "single-primary",
                            "connections": [
                                {
                                    "connection_id": 1,
                                    "endpoint_name": "openai-main",
                                }
                            ],
                        }
                    ],
                    "user_settings": {
                        "endpoint_fx_mappings": [
                            {
                                "model_id": "gpt-4o",
                                "endpoint_id": 1,
                                "endpoint_name": "openai-main",
                                "fx_rate": "1",
                            }
                        ]
                    },
                }
            )

        assert "Extra inputs are not permitted" in str(exc_info.value)

    def test_validate_import_accepts_duplicate_connection_names_across_models(self):
        from app.routers.config import _validate_import
        from app.schemas.schemas import ConfigImportRequest

        data = ConfigImportRequest.model_validate(
            {
                "version": 3,
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
                        "name": "single-primary",
                        "strategy_type": "single",
                        "failover_recovery_enabled": False,
                    }
                ],
                "models": [
                    {
                        "provider_type": "openai",
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
                        "provider_type": "openai",
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

    def test_validate_import_accepts_name_references_without_ids(self):
        from app.routers.config import _validate_import
        from app.schemas.schemas import ConfigImportRequest

        data = ConfigImportRequest.model_validate(
            {
                "version": 3,
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
                        "name": "single-primary",
                        "strategy_type": "single",
                        "failover_recovery_enabled": False,
                    }
                ],
                "models": [
                    {
                        "provider_type": "openai",
                        "model_id": "gpt-4o",
                        "model_type": "native",
                        "loadbalance_strategy_name": "single-primary",
                        "connections": [
                            {
                                "endpoint_name": "openai-main",
                            }
                        ],
                    }
                ],
                "user_settings": {
                    "endpoint_fx_mappings": [
                        {
                            "model_id": "gpt-4o",
                            "endpoint_name": "openai-main",
                            "fx_rate": "1",
                        }
                    ]
                },
            }
        )

        _validate_import(data)
