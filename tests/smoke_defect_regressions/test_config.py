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
            "endpoint_id",
            "name",
            "base_url",
            "api_key",
        }
        assert expected.issubset(fields), f"Missing fields: {expected - fields}"

    def test_export_schema_includes_all_connection_fields(self):
        from app.schemas.schemas import ConfigConnectionExport

        fields = set(ConfigConnectionExport.model_fields.keys())
        expected = {
            "connection_id",
            "endpoint_id",
            "pricing_template_id",
            "is_active",
            "priority",
            "name",
            "auth_type",
            "custom_headers",
        }
        assert expected.issubset(fields), f"Missing fields: {expected - fields}"


    def test_export_schema_includes_all_model_fields(self):
        from app.schemas.schemas import ConfigModelExport

        fields = set(ConfigModelExport.model_fields.keys())
        expected = {
            "provider_type",
            "model_id",
            "display_name",
            "model_type",
            "redirect_to",
            "lb_strategy",
            "failover_recovery_enabled",
            "failover_recovery_cooldown_seconds",
            "is_enabled",
            "connections",
        }
        assert expected.issubset(fields), f"Missing fields: {expected - fields}"

    def test_roundtrip_custom_headers_preserved(self):
        from app.schemas.schemas import ConfigConnectionExport

        headers = {"X-Custom": "value", "X-Another": "test"}
        connection = ConfigConnectionExport(
            endpoint_id=1,
            connection_id=1,
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
            endpoint_id=1,
            connection_id=2,
            custom_headers=None,
        )
        exported = connection.model_dump(mode="json")
        reimported = ConfigConnectionExport(**exported)
        assert reimported.custom_headers is None

    def test_roundtrip_custom_headers_empty_dict(self):
        from app.schemas.schemas import ConfigConnectionExport

        connection = ConfigConnectionExport(
            endpoint_id=1,
            connection_id=3,
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
                    failover_recovery_enabled=True,
                    failover_recovery_cooldown_seconds=60,
                    connections=[
                        ConfigConnectionExport(
                            connection_id=1,
                            endpoint_id=1,
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
        assert m.lb_strategy == "failover"
        assert m.failover_recovery_enabled is True
        assert m.failover_recovery_cooldown_seconds == 60
        assert len(m.connections) == 1
        connection = m.connections[0]
        assert connection.custom_headers == {"X-Org": "my-org"}
        assert connection.auth_type == "openai"
        assert connection.priority == 0

class TestDEF023_ConfigImportReferenceValidation:
    def test_validate_import_accepts_numeric_ids(self):
        from app.schemas.schemas import ConfigImportRequest
        from app.routers.config import _validate_import

        data = ConfigImportRequest.model_validate(
            {
                "version": 2,
                "endpoints": [
                    {
                        "endpoint_id": 1,
                        "name": "openai-main",
                        "base_url": "https://api.openai.com/v1",
                        "api_key": "sk-test",
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
                    }
                ],
                "user_settings": {
                    "endpoint_fx_mappings": [
                        {
                            "model_id": "gpt-4o",
                            "endpoint_id": 1,
                            "fx_rate": "1",
                        }
                    ]
                },
            }
        )

        _validate_import(data)

    def test_validate_import_rejects_duplicate_connection_ids(self):
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

class TestDEF024_ConfigImportExportRefRoundtrip:
    @pytest.mark.asyncio
    async def test_import_export_roundtrip_preserves_numeric_ids(self):
        from sqlalchemy import select
        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import Connection, Endpoint, EndpointFxRateSetting, Profile, Provider
        from app.routers.config import export_config, import_config
        from app.schemas.schemas import ConfigImportRequest

        # Prevent cross-loop pooled asyncpg connections from previous tests.
        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        endpoint_name = f"def024-endpoint-{suffix}"
        model_id = f"def024-model-{suffix}"
        connection_name = f"def024-connection-{suffix}"
        payload = ConfigImportRequest.model_validate(
            {
                "version": 2,
                "endpoints": [
                    {
                        "endpoint_id": 9001,
                        "name": endpoint_name,
                        "base_url": "https://api.openai.com/v1",
                        "api_key": "sk-test",
                    }
                ],
                "pricing_templates": [],
                "models": [
                    {
                        "provider_type": "openai",
                        "model_id": model_id,
                        "model_type": "native",
                        "connections": [
                            {
                                "connection_id": 7001,
                                "endpoint_id": 9001,
                                "name": connection_name,
                            }
                        ],
                    }
                ],
                "user_settings": {
                    "report_currency_code": "USD",
                    "report_currency_symbol": "$",
                    "endpoint_fx_mappings": [
                        {
                            "model_id": model_id,
                            "endpoint_id": 9001,
                            "fx_rate": "1.25",
                        }
                    ],
                },
            }
        )

        async with AsyncSessionLocal() as db:
            existing_profile = await db.get(Profile, 1)
            if existing_profile is None:
                db.add(
                    Profile(
                        id=1,
                        name="DEF024 Profile",
                        is_active=False,
                        version=0,
                    )
                )
                await db.flush()
            provider = (
                await db.execute(
                    select(Provider)
                    .where(Provider.provider_type == "openai")
                    .order_by(Provider.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if provider is None:
                db.add(
                    Provider(
                        name=f"DEF024 OpenAI {suffix}",
                        provider_type="openai",
                    )
                )
                await db.flush()
            response = await import_config(data=payload, db=db, profile_id=1)
            await db.commit()
            assert response.endpoints_imported == 1
            assert response.connections_imported == 1

        async with AsyncSessionLocal() as db:
            endpoint = (
                await db.execute(
                    select(Endpoint).where(
                        Endpoint.profile_id == 1,
                        Endpoint.name == endpoint_name,
                    )
                )
            ).scalar_one()
            connection = (
                await db.execute(
                    select(Connection).where(
                        Connection.profile_id == 1,
                        Connection.name == connection_name,
                    )
                )
            ).scalar_one()
            fx_row = (
                await db.execute(
                    select(EndpointFxRateSetting).where(
                        EndpointFxRateSetting.profile_id == 1,
                        EndpointFxRateSetting.model_id == model_id,
                        EndpointFxRateSetting.endpoint_id == endpoint.id,
                    )
                )
            ).scalar_one_or_none()

            assert isinstance(endpoint.id, int) and endpoint.id > 0
            assert isinstance(connection.id, int) and connection.id > 0
            assert connection.endpoint_id == endpoint.id
            assert fx_row is not None

            export_response = await export_config(db=db, profile_id=1)
            exported = json.loads(export_response.body)

        exported_endpoint = next(
            e for e in exported["endpoints"] if e["name"] == endpoint_name
        )
        assert exported_endpoint["endpoint_id"] == endpoint.id

        exported_model = next(
            m for m in exported["models"] if m["model_id"] == model_id
        )
        exported_connection = next(
            c for c in exported_model["connections"] if c["name"] == connection_name
        )
        assert exported_connection["endpoint_id"] == endpoint.id
        assert exported_connection["connection_id"] == connection.id

        exported_mapping = next(
            m
            for m in exported["user_settings"]["endpoint_fx_mappings"]
            if m["model_id"] == model_id
        )
        assert exported_mapping["endpoint_id"] == endpoint.id

class TestDEF026_ConfigImportSystemRuleTimestamp:
    @pytest.mark.asyncio
    async def test_import_updates_system_rules_without_timezone_errors(self):
        from app.core.database import AsyncSessionLocal, get_engine
        from app.routers.config import import_config
        from app.schemas.schemas import ConfigImportRequest

        # Prevent cross-loop pooled asyncpg connections from previous tests.
        await get_engine().dispose()

        async with AsyncSessionLocal() as db:
            from app.main import SYSTEM_BLOCKLIST_DEFAULTS
            from app.models.models import Profile

            system_rule = SYSTEM_BLOCKLIST_DEFAULTS[0]

            existing_profile = await db.get(Profile, 1)
            if existing_profile is None:
                db.add(
                    Profile(
                        id=1,
                        name="DEF026 Profile",
                        is_active=False,
                        version=0,
                    )
                )
                await db.flush()
            payload = ConfigImportRequest.model_validate(
                {
                    "version": 2,
                    "endpoints": [],
                    "models": [],
                    "pricing_templates": [],
                    "user_settings": {
                        "report_currency_code": "USD",
                        "report_currency_symbol": "$",
                        "endpoint_fx_mappings": [],
                    },
                    "header_blocklist_rules": [
                        {
                            "name": system_rule["name"],
                            "match_type": system_rule["match_type"],
                            "pattern": system_rule["pattern"],
                            "enabled": True,
                        }
                    ],
                }
            )

            response = await import_config(data=payload, db=db, profile_id=1)

            assert response.endpoints_imported == 0
            assert response.models_imported == 0
            assert response.connections_imported == 0
            assert response.connections_imported == 0

            await db.rollback()

