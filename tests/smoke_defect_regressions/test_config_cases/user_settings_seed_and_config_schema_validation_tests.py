import asyncio
import json
import logging
from typing import AsyncGenerator, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.services.proxy_service import (
    extract_model_from_body,
    build_upstream_headers,
)
from app.services.stats_service import log_request
from tests.loadbalance_strategy_helpers import (
    make_routing_policy_adaptive,
)


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


class TestDEF080_VendorCatalogManagementSurface:
    @pytest.mark.asyncio
    async def test_seed_vendors_creates_default_global_vendor_catalog(self):
        from app.main import seed_vendors
        from app.models.models import Vendor

        existing_result = MagicMock()
        existing_scalars = MagicMock()
        existing_scalars.all.return_value = []
        existing_result.scalars.return_value = existing_scalars

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock(return_value=existing_result)

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "app.core.database.AsyncSessionLocal",
            return_value=mock_session_ctx,
        ):
            await seed_vendors()

        seeded_vendors = [call.args[0] for call in mock_session.add.call_args_list]
        assert all(isinstance(vendor, Vendor) for vendor in seeded_vendors)
        assert [
            (vendor.key, vendor.name, vendor.icon_key) for vendor in seeded_vendors
        ] == [
            ("openai", "OpenAI", "openai"),
            ("anthropic", "Anthropic", "anthropic"),
            ("google", "Google", "google"),
        ]
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_vendors_router_lists_seeded_global_catalog(self):
        from app.models.models import Vendor
        from app.routers.vendors import list_vendors, router

        result = MagicMock()
        scalars = MagicMock()
        scalars.all.return_value = [
            Vendor(key="openai", name="OpenAI", icon_key="openai"),
            Vendor(key="anthropic", name="Anthropic", icon_key="anthropic"),
            Vendor(key="google", name="Google", icon_key="google"),
        ]
        result.scalars.return_value = scalars

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=result)

        vendors = await list_vendors(db=mock_db)

        assert router.prefix == "/api/vendors"
        assert [vendor.key for vendor in vendors] == ["openai", "anthropic", "google"]
        assert [vendor.icon_key for vendor in vendors] == [
            "openai",
            "anthropic",
            "google",
        ]

    def test_main_app_mounts_vendors_router(self):
        from app.main import app

        route_paths = {
            path
            for route in app.routes
            if (path := getattr(route, "path", None)) is not None
        }

        removed_route_prefix = "/api/" + "providers"
        assert "/api/vendors" in route_paths
        assert not any(path.startswith(removed_route_prefix) for path in route_paths)


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
            "openai_probe_endpoint_variant",
            "qps_limit",
            "max_in_flight_non_stream",
            "max_in_flight_stream",
        }
        assert expected.issubset(fields), f"Missing fields: {expected - fields}"

    def test_connection_response_schema_includes_limiter_fields(self):
        from app.schemas.schemas import ConnectionResponse

        fields = set(ConnectionResponse.model_fields.keys())
        expected = {
            "monitoring_probe_interval_seconds",
            "openai_probe_endpoint_variant",
            "qps_limit",
            "max_in_flight_non_stream",
            "max_in_flight_stream",
        }
        assert expected.issubset(fields), f"Missing fields: {expected - fields}"

    def test_config_export_schema_defaults_to_version_1(self):
        from app.schemas.schemas import ConfigExportResponse

        assert ConfigExportResponse.model_fields["version"].default == 1

    def test_export_schema_includes_top_level_vendors_field(self):
        from app.schemas.schemas import ConfigExportResponse

        fields = set(ConfigExportResponse.model_fields.keys())
        assert "vendors" in fields

    def test_export_schema_includes_all_model_fields(self):
        from app.schemas.schemas import ConfigModelExport

        fields = set(ConfigModelExport.model_fields.keys())
        expected = {
            "vendor_key",
            "api_family",
            "model_id",
            "display_name",
            "model_type",
            "proxy_targets",
            "loadbalance_strategy_name",
            "is_enabled",
            "connections",
        }
        assert expected.issubset(fields), f"Missing fields: {expected - fields}"

    def test_request_log_response_schema_includes_resolved_target_model_id(self):
        from app.schemas.schemas import RequestLogResponse

        fields = set(RequestLogResponse.model_fields.keys())
        assert "resolved_target_model_id" in fields

    def test_export_schema_includes_all_loadbalance_strategy_fields(self):
        from app.schemas.schemas import ConfigLoadbalanceStrategyExport

        fields = set(ConfigLoadbalanceStrategyExport.model_fields.keys())
        expected = {
            "name",
            "routing_policy",
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
            ConfigVendorExport,
        )
        from datetime import datetime, timezone

        config = ConfigExportResponse(
            exported_at=datetime.now(timezone.utc),
            vendors=[
                ConfigVendorExport(
                    key="openai",
                    name="OpenAI",
                    description="OpenAI API (GPT models)",
                    icon_key="openai",
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
                    strategy_type="adaptive",
                    routing_policy=make_routing_policy_adaptive(
                        routing_objective="maximize_availability",
                        failure_status_codes=[503, 429],
                        base_open_seconds=45,
                        failure_threshold=4,
                        backoff_multiplier=3.5,
                        max_open_seconds=720,
                        jitter_ratio=0.35,
                    ),
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
                            name="Primary",
                            auth_type="openai",
                            custom_headers={"X-Org": "my-org"},
                            openai_probe_endpoint_variant="responses_reasoning_none",
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
        assert m.vendor_key == "openai"
        assert m.api_family == "openai"
        assert m.loadbalance_strategy_name == "failover-primary"
        strategy = reimported.loadbalance_strategies[0]
        assert strategy.routing_policy.routing_objective == "maximize_availability"
        assert strategy.routing_policy.circuit_breaker.base_open_seconds == 45
        assert strategy.routing_policy.circuit_breaker.failure_threshold == 4
        assert strategy.routing_policy.circuit_breaker.backoff_multiplier == 3.5
        assert strategy.routing_policy.circuit_breaker.max_open_seconds == 720
        assert strategy.routing_policy.circuit_breaker.jitter_ratio == 0.35
        assert strategy.routing_policy.circuit_breaker.failure_status_codes == [
            429,
            503,
        ]
        assert len(m.connections) == 1
        connection = m.connections[0]
        assert connection.custom_headers == {"X-Org": "my-org"}
        assert connection.auth_type == "openai"
        assert connection.openai_probe_endpoint_variant == "responses_reasoning_none"
        assert connection.priority == 0
        assert connection.endpoint_name == "openai-main"
        assert reimported.vendors[0].key == "openai"
        assert reimported.vendors[0].icon_key == "openai"
        assert "icon_key" not in exported["models"][0]

    def test_version_1_import_schema_accepts_fill_first_strategy(self):
        from app.schemas.schemas import ConfigImportRequest

        validation = ConfigImportRequest.model_validate(
            {
                "version": 1,
                "vendors": [
                    {
                        "key": "openai",
                        "name": "OpenAI",
                        "description": "OpenAI API (GPT models)",
                        "icon_key": "openai",
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
                        "name": "adaptive-availability",
                        "strategy_type": "adaptive",
                        "routing_policy": make_routing_policy_adaptive(
                            routing_objective="maximize_availability",
                            failure_status_codes=[503, 429],
                            base_open_seconds=45,
                            failure_threshold=4,
                            backoff_multiplier=3.5,
                            max_open_seconds=720,
                            jitter_ratio=0.35,
                            ban_mode="temporary",
                            max_open_strikes_before_ban=3,
                            ban_duration_seconds=600,
                        ),
                    }
                ],
                "models": [
                    {
                        "vendor_key": "openai",
                        "api_family": "openai",
                        "model_id": "gpt-4o",
                        "model_type": "native",
                        "loadbalance_strategy_name": "adaptive-availability",
                        "connections": [{"endpoint_name": "openai-main"}],
                    }
                ],
            }
        )

        strategy = validation.loadbalance_strategies[0]
        assert strategy.routing_policy.routing_objective == "maximize_availability"
        assert strategy.routing_policy.circuit_breaker.ban_mode == "temporary"
        assert strategy.routing_policy.circuit_breaker.max_open_strikes_before_ban == 3
        assert strategy.routing_policy.circuit_breaker.ban_duration_seconds == 600

    def test_config_import_schema_accepts_nullable_vendor_icon_key(self):
        from app.schemas.schemas import ConfigImportRequest

        payload = ConfigImportRequest.model_validate(
            {
                "version": 1,
                "vendors": [
                    {
                        "key": "openai",
                        "name": "OpenAI",
                        "description": "OpenAI API (GPT models)",
                        "icon_key": None,
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
                        "name": "single-primary",
                        "strategy_type": "adaptive",
                        "routing_policy": make_routing_policy_adaptive(),
                    }
                ],
                "models": [
                    {
                        "vendor_key": "openai",
                        "api_family": "openai",
                        "model_id": "gpt-4o",
                        "model_type": "native",
                        "loadbalance_strategy_name": "single-primary",
                        "connections": [{"endpoint_name": "openai-main"}],
                    }
                ],
            }
        )

        assert payload.vendors[0].icon_key is None

    def test_config_import_schema_rejects_vendor_objects_that_omit_icon_key(self):
        from app.schemas.schemas import ConfigImportRequest

        with pytest.raises(ValidationError, match="icon_key"):
            ConfigImportRequest.model_validate(
                {
                    "version": 1,
                    "vendors": [
                        {
                            "key": "openrouter",
                            "name": "OpenRouter",
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
                            "name": "single-primary",
                            "routing_policy": make_routing_policy_adaptive(),
                        }
                    ],
                    "models": [
                        {
                            "vendor_key": "openrouter",
                            "api_family": "openai",
                            "model_id": "gpt-4o",
                            "model_type": "native",
                            "loadbalance_strategy_name": "single-primary",
                            "connections": [{"endpoint_name": "openai-main"}],
                        }
                    ],
                }
            )

    def test_config_import_schema_rejects_unsupported_version_numbers(self):
        from app.schemas.schemas import ConfigImportRequest

        with pytest.raises(ValidationError, match="1"):
            ConfigImportRequest.model_validate(
                {
                    "version": 2,
                    "vendors": [
                        {
                            "key": "openai",
                            "name": "OpenAI",
                            "description": "OpenAI API (GPT models)",
                            "icon_key": "openai",
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
                            "name": "single-primary",
                            "routing_policy": make_routing_policy_adaptive(),
                        }
                    ],
                    "models": [
                        {
                            "vendor_key": "openai",
                            "api_family": "openai",
                            "model_id": "gpt-4o",
                            "model_type": "native",
                            "loadbalance_strategy_name": "single-primary",
                            "connections": [{"endpoint_name": "openai-main"}],
                        }
                    ],
                }
            )

    @pytest.mark.asyncio
    async def test_import_route_rejects_version_two_with_literal_error_detail(self):
        from httpx import ASGITransport, AsyncClient

        from app.core.database import AsyncSessionLocal, get_engine
        from app.main import app
        from app.models.models import Profile

        await get_engine().dispose()

        async with AsyncSessionLocal() as db:
            profile = Profile(
                name=f"DEF089 Config Version Gate {int(asyncio.get_running_loop().time() * 1_000_000)}",
                is_active=False,
                is_default=False,
                is_editable=True,
                version=0,
            )
            db.add(profile)
            await db.commit()
            await db.refresh(profile)
            profile_id = profile.id

        transport = ASGITransport(app=app)

        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/config/import",
                headers={"X-Profile-Id": str(profile_id)},
                json={
                    "version": 2,
                    "vendors": [],
                    "endpoints": [],
                    "pricing_templates": [],
                    "loadbalance_strategies": [],
                    "models": [],
                },
            )

        assert response.status_code == 422
        assert response.json()["detail"] == [
            {
                "type": "literal_error",
                "loc": ["body", "version"],
                "msg": "Input should be 1",
                "input": 2,
                "ctx": {"expected": "1"},
            }
        ]


class TestDEF023_ConfigImportReferenceValidation:
    def test_validate_import_rejects_numeric_ids(self):
        from app.schemas.schemas import ConfigImportRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            ConfigImportRequest.model_validate(
                {
                    "version": 1,
                    "vendors": [
                        {
                            "key": "openai",
                            "name": "OpenAI",
                            "description": "OpenAI API (GPT models)",
                            "icon_key": "openai",
                            "audit_enabled": False,
                            "audit_capture_bodies": True,
                        }
                    ],
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
                            "routing_policy": make_routing_policy_adaptive(),
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
                "version": 1,
                "vendors": [
                    {
                        "key": "openai",
                        "name": "OpenAI",
                        "description": "OpenAI API (GPT models)",
                        "icon_key": "openai",
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
                        "name": "single-primary",
                        "strategy_type": "adaptive",
                        "routing_policy": make_routing_policy_adaptive(),
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

    def test_validate_import_accepts_name_references_without_ids(self):
        from app.routers.config import _validate_import
        from app.schemas.schemas import ConfigImportRequest

        data = ConfigImportRequest.model_validate(
            {
                "version": 1,
                "vendors": [
                    {
                        "key": "openai",
                        "name": "OpenAI",
                        "description": "OpenAI API (GPT models)",
                        "icon_key": "openai",
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
                        "name": "single-primary",
                        "strategy_type": "adaptive",
                        "routing_policy": make_routing_policy_adaptive(),
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
