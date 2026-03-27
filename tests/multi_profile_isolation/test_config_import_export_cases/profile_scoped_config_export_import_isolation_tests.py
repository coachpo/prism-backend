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
    Vendor,
    ModelConfig,
    Endpoint,
    Connection,
    LoadbalanceStrategy,
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
from tests.loadbalance_strategy_helpers import make_loadbalance_strategy


class TestConfigExportImportIsolation:
    """FR-007: Config Export/Import Isolation"""

    @pytest.mark.asyncio
    async def test_export_config_filters_by_profile(self):
        """Config export returns only data for the specified profile."""
        from app.routers.config import export_config

        mock_db = AsyncMock()

        now = datetime.now(timezone.utc)
        endpoint = SimpleNamespace(
            id=10,
            profile_id=1,
            name="openai-main",
            base_url="https://api.openai.com",
            api_key="sk-test",
            position=0,
        )
        model = SimpleNamespace(
            id=20,
            profile_id=1,
            vendor_id=1,
            api_family="openai",
            model_id="gpt-4",
            display_name=None,
            model_type="native",
            proxy_targets=[],
            loadbalance_strategy_id=11,
            loadbalance_strategy=SimpleNamespace(
                id=11,
                name="fill-first-primary",
                strategy_type="fill-first",
                failover_recovery_enabled=True,
                failover_cooldown_seconds=45,
                failover_failure_threshold=4,
                failover_backoff_multiplier=3.5,
                failover_max_cooldown_seconds=720,
                failover_jitter_ratio=0.35,
                failover_status_codes=[403, 422, 429, 500, 502, 503, 504, 529],
                failover_ban_mode="off",
                failover_max_cooldown_strikes_before_ban=0,
                failover_ban_duration_seconds=0,
            ),
            is_enabled=True,
            connections=[
                SimpleNamespace(
                    id=30,
                    endpoint_id=endpoint.id,
                    endpoint_rel=endpoint,
                    pricing_template_id=None,
                    pricing_template_rel=None,
                    is_active=True,
                    priority=0,
                    name="primary",
                    auth_type=None,
                    custom_headers=None,
                    qps_limit=3,
                    max_in_flight_non_stream=5,
                    max_in_flight_stream=2,
                )
            ],
            created_at=now,
            updated_at=now,
        )
        proxy_model = SimpleNamespace(
            id=21,
            profile_id=1,
            vendor_id=1,
            api_family="openai",
            model_id="gpt-4-proxy",
            display_name="GPT-4 Proxy",
            model_type="proxy",
            loadbalance_strategy_id=None,
            loadbalance_strategy=None,
            proxy_targets=[
                SimpleNamespace(target_model_id="gpt-4", position=0),
            ],
            is_enabled=True,
            connections=[],
            created_at=now,
            updated_at=now,
        )

        # Mock query results
        endpoint_result = MagicMock()
        endpoint_result.scalars.return_value.all.return_value = [endpoint]

        model_result = MagicMock()
        model_result.scalars.return_value.all.return_value = [model, proxy_model]

        pricing_templates_result = MagicMock()
        pricing_templates_result.scalars.return_value.all.return_value = []

        strategies_result = MagicMock()
        strategies_result.scalars.return_value.all.return_value = [
            model.loadbalance_strategy
        ]

        user_settings_result = MagicMock()
        user_settings_result.scalar_one_or_none.return_value = None

        fx_result = MagicMock()
        fx_result.scalars.return_value.all.return_value = []

        blocklist_result = MagicMock()
        blocklist_result.scalars.return_value.all.return_value = []

        vendors_result = MagicMock()
        vendors_result.scalars.return_value.all.return_value = [
            SimpleNamespace(
                id=1,
                key="openai",
                name="OpenAI",
                description="OpenAI API (GPT models)",
                icon_key="openai",
                audit_enabled=False,
                audit_capture_bodies=True,
            )
        ]

        mock_db.execute.side_effect = [
            endpoint_result,
            model_result,
            strategies_result,
            pricing_templates_result,
            vendors_result,
            user_settings_result,
            fx_result,
            blocklist_result,
        ]

        config = await export_config(db=mock_db, profile_id=1)
        payload = json.loads(bytes(config.body).decode("utf-8"))

        # Verify export contains profile 1 data only
        assert payload["version"] == 8
        assert payload["vendors"] == [
            {
                "key": "openai",
                "name": "OpenAI",
                "description": "OpenAI API (GPT models)",
                "icon_key": "openai",
                "audit_enabled": False,
                "audit_capture_bodies": True,
            }
        ]
        assert len(payload["endpoints"]) == 1
        assert len(payload["loadbalance_strategies"]) == 1
        assert len(payload["models"]) == 2
        strategy_payload = payload["loadbalance_strategies"][0]
        assert strategy_payload["strategy_type"] == "fill-first"
        assert strategy_payload["failover_recovery_enabled"] is True
        assert strategy_payload["failover_cooldown_seconds"] == 45
        assert strategy_payload["failover_failure_threshold"] == 4
        assert strategy_payload["failover_backoff_multiplier"] == 3.5
        assert strategy_payload["failover_max_cooldown_seconds"] == 720
        assert strategy_payload["failover_jitter_ratio"] == 0.35
        assert strategy_payload["failover_status_codes"] == [
            403,
            422,
            429,
            500,
            502,
            503,
            504,
            529,
        ]
        assert strategy_payload["failover_ban_mode"] == "off"
        assert strategy_payload["failover_max_cooldown_strikes_before_ban"] == 0
        assert strategy_payload["failover_ban_duration_seconds"] == 0
        exported_connection = payload["models"][0]["connections"][0]
        assert exported_connection["qps_limit"] == 3
        assert exported_connection["max_in_flight_non_stream"] == 5
        assert exported_connection["max_in_flight_stream"] == 2
        assert payload["models"][0]["vendor_key"] == "openai"
        assert payload["models"][0]["api_family"] == "openai"
        exported_proxy = next(
            item for item in payload["models"] if item["model_type"] == "proxy"
        )
        assert exported_proxy["proxy_targets"] == [
            {"target_model_id": "gpt-4", "position": 0}
        ]
        assert all(
            "icon_key" not in exported_model for exported_model in payload["models"]
        )
        assert "redirect_to" not in exported_proxy
        assert "providers" not in payload

    @pytest.mark.asyncio
    async def test_import_config_replaces_target_profile_only(self):
        """Config import replace mode mutates only the target profile's rows."""
        from app.core.database import AsyncSessionLocal, get_engine
        from app.routers.config import import_config
        from app.schemas.schemas import ConfigImportRequest

        await get_engine().dispose()

        suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        target_profile_name = f"import-target-{suffix}"
        other_profile_name = f"import-other-{suffix}"

        old_target_endpoint_name = f"target-endpoint-old-{suffix}"
        old_target_model_id = f"target-model-old-{suffix}"
        old_target_connection_name = f"target-connection-old-{suffix}"
        old_target_rule_pattern = f"x-target-old-{suffix}"

        other_endpoint_name = f"other-endpoint-{suffix}"
        other_model_id = f"other-model-{suffix}"
        other_connection_name = f"other-connection-{suffix}"
        other_rule_pattern = f"x-other-{suffix}"

        new_endpoint_name = f"target-endpoint-new-{suffix}"
        new_endpoint_id = 9001
        new_model_id = f"target-model-new-{suffix}"
        new_connection_name = f"target-connection-new-{suffix}"
        new_connection_id = 7001
        new_rule_pattern = f"x-target-new-{suffix}"

        async with AsyncSessionLocal() as db:
            openai_vendor = (
                await db.execute(
                    select(Vendor)
                    .where(Vendor.key == "openai")
                    .order_by(Vendor.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if openai_vendor is None:
                openai_vendor = Vendor(
                    key="openai",
                    name="OpenAI",
                    description="OpenAI API (GPT models)",
                )
                db.add(openai_vendor)
                await db.flush()

            openrouter_vendor = (
                await db.execute(
                    select(Vendor)
                    .where(Vendor.key == "openrouter")
                    .order_by(Vendor.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if openrouter_vendor is None:
                openrouter_vendor = Vendor(
                    key="openrouter",
                    name="OpenRouter",
                    description="OpenRouter global catalog entry",
                    icon_key="openrouter",
                    audit_enabled=True,
                    audit_capture_bodies=False,
                )
                db.add(openrouter_vendor)
                await db.flush()

            target_profile = Profile(
                name=target_profile_name,
                description="Target profile for replace import",
                is_active=False,
                is_default=False,
                is_editable=True,
                version=0,
            )
            other_profile = Profile(
                name=other_profile_name,
                description="Control profile that must stay unchanged",
                is_active=False,
                is_default=False,
                is_editable=True,
                version=0,
            )
            db.add_all([target_profile, other_profile])
            await db.flush()

            target_endpoint = Endpoint(
                profile_id=target_profile.id,
                name=old_target_endpoint_name,
                base_url="https://api.openai.com",
                api_key="sk-target-old",
                position=0,
            )
            other_endpoint = Endpoint(
                profile_id=other_profile.id,
                name=other_endpoint_name,
                base_url="https://api.openai.com",
                api_key="sk-other",
                position=0,
            )
            db.add_all([target_endpoint, other_endpoint])
            await db.flush()

            target_model = ModelConfig(
                profile_id=target_profile.id,
                vendor_id=openai_vendor.id,
                api_family="openai",
                model_id=old_target_model_id,
                model_type="native",
                loadbalance_strategy=make_loadbalance_strategy(
                    profile_id=target_profile.id,
                    strategy_type="single",
                ),
                is_enabled=True,
            )
            other_model = ModelConfig(
                profile_id=other_profile.id,
                vendor_id=openai_vendor.id,
                api_family="openai",
                model_id=other_model_id,
                model_type="native",
                loadbalance_strategy=make_loadbalance_strategy(
                    profile_id=other_profile.id,
                    strategy_type="single",
                ),
                is_enabled=True,
            )
            db.add_all([target_model, other_model])
            await db.flush()

            db.add_all(
                [
                    Connection(
                        profile_id=target_profile.id,
                        model_config_id=target_model.id,
                        endpoint_id=target_endpoint.id,
                        is_active=True,
                        priority=0,
                        name=old_target_connection_name,
                    ),
                    Connection(
                        profile_id=other_profile.id,
                        model_config_id=other_model.id,
                        endpoint_id=other_endpoint.id,
                        is_active=True,
                        priority=0,
                        name=other_connection_name,
                    ),
                    UserSetting(
                        profile_id=target_profile.id,
                        report_currency_code="USD",
                        report_currency_symbol="$",
                    ),
                    UserSetting(
                        profile_id=other_profile.id,
                        report_currency_code="EUR",
                        report_currency_symbol="EUR",
                    ),
                    EndpointFxRateSetting(
                        profile_id=target_profile.id,
                        model_id=old_target_model_id,
                        endpoint_id=target_endpoint.id,
                        fx_rate="1.10",
                    ),
                    EndpointFxRateSetting(
                        profile_id=other_profile.id,
                        model_id=other_model_id,
                        endpoint_id=other_endpoint.id,
                        fx_rate="1.50",
                    ),
                    HeaderBlocklistRule(
                        profile_id=target_profile.id,
                        name=f"target-rule-old-{suffix}",
                        match_type="exact",
                        pattern=old_target_rule_pattern,
                        enabled=True,
                        is_system=False,
                    ),
                    HeaderBlocklistRule(
                        profile_id=other_profile.id,
                        name=f"other-rule-{suffix}",
                        match_type="exact",
                        pattern=other_rule_pattern,
                        enabled=True,
                        is_system=False,
                    ),
                ]
            )
            await db.commit()

            target_profile_id = target_profile.id
            other_profile_id = other_profile.id
            openai_vendor_id = openai_vendor.id
            openrouter_vendor_id = openrouter_vendor.id

        payload = ConfigImportRequest.model_validate(
            {
                "version": 8,
                "vendors": [
                    {
                        "key": "openrouter",
                        "name": "OpenRouter",
                        "description": "OpenRouter global catalog entry",
                        "icon_key": "openrouter",
                        "audit_enabled": True,
                        "audit_capture_bodies": False,
                    }
                ],
                "endpoints": [
                    {
                        "name": new_endpoint_name,
                        "base_url": "https://api.openai.com",
                        "api_key": "sk-target-new",
                    }
                ],
                "pricing_templates": [],
                "loadbalance_strategies": [
                    {
                        "name": "fill-first-primary",
                        "strategy_type": "fill-first",
                        "failover_recovery_enabled": True,
                        "failover_cooldown_seconds": 45,
                        "failover_failure_threshold": 4,
                        "failover_backoff_multiplier": 3.5,
                        "failover_max_cooldown_seconds": 720,
                        "failover_jitter_ratio": 0.35,
                        "failover_status_codes": [
                            403,
                            422,
                            429,
                            500,
                            502,
                            503,
                            504,
                            529,
                        ],
                        "failover_ban_mode": "temporary",
                        "failover_max_cooldown_strikes_before_ban": 2,
                        "failover_ban_duration_seconds": 600,
                    }
                ],
                "models": [
                    {
                        "vendor_key": "openrouter",
                        "api_family": "openai",
                        "model_id": new_model_id,
                        "model_type": "native",
                        "loadbalance_strategy_name": "fill-first-primary",
                        "connections": [
                            {
                                "endpoint_name": new_endpoint_name,
                                "name": new_connection_name,
                                "priority": 0,
                                "is_active": True,
                                "qps_limit": 7,
                                "max_in_flight_non_stream": 9,
                                "max_in_flight_stream": 4,
                            }
                        ],
                    }
                ],
                "user_settings": {
                    "report_currency_code": "GBP",
                    "report_currency_symbol": "GBP",
                    "endpoint_fx_mappings": [
                        {
                            "model_id": new_model_id,
                            "endpoint_name": new_endpoint_name,
                            "fx_rate": "1.25",
                        }
                    ],
                },
                "header_blocklist_rules": [
                    {
                        "name": f"target-rule-new-{suffix}",
                        "match_type": "exact",
                        "pattern": new_rule_pattern,
                        "enabled": True,
                    }
                ],
            }
        )

        async with AsyncSessionLocal() as db:
            response = await import_config(
                data=payload,
                db=db,
                profile_id=target_profile_id,
            )
            await db.commit()

            assert response.strategies_imported == 1
            assert response.endpoints_imported == 1
            assert response.models_imported == 1
            assert response.connections_imported == 1

        async with AsyncSessionLocal() as db:
            target_endpoints = (
                (
                    await db.execute(
                        select(Endpoint).where(Endpoint.profile_id == target_profile_id)
                    )
                )
                .scalars()
                .all()
            )
            target_models = (
                (
                    await db.execute(
                        select(ModelConfig).where(
                            ModelConfig.profile_id == target_profile_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            target_strategies = (
                (
                    await db.execute(
                        select(LoadbalanceStrategy).where(
                            LoadbalanceStrategy.profile_id == target_profile_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            target_connections = (
                (
                    await db.execute(
                        select(Connection).where(
                            Connection.profile_id == target_profile_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            target_fx_rows = (
                (
                    await db.execute(
                        select(EndpointFxRateSetting).where(
                            EndpointFxRateSetting.profile_id == target_profile_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            target_rules = (
                (
                    await db.execute(
                        select(HeaderBlocklistRule).where(
                            HeaderBlocklistRule.profile_id == target_profile_id,
                            HeaderBlocklistRule.is_system == False,  # noqa: E712
                        )
                    )
                )
                .scalars()
                .all()
            )
            target_settings = (
                await db.execute(
                    select(UserSetting).where(
                        UserSetting.profile_id == target_profile_id
                    )
                )
            ).scalar_one()

            other_endpoints = (
                (
                    await db.execute(
                        select(Endpoint).where(Endpoint.profile_id == other_profile_id)
                    )
                )
                .scalars()
                .all()
            )
            other_models = (
                (
                    await db.execute(
                        select(ModelConfig).where(
                            ModelConfig.profile_id == other_profile_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            other_connections = (
                (
                    await db.execute(
                        select(Connection).where(
                            Connection.profile_id == other_profile_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            other_fx_rows = (
                (
                    await db.execute(
                        select(EndpointFxRateSetting).where(
                            EndpointFxRateSetting.profile_id == other_profile_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            other_rules = (
                (
                    await db.execute(
                        select(HeaderBlocklistRule).where(
                            HeaderBlocklistRule.profile_id == other_profile_id,
                            HeaderBlocklistRule.is_system == False,  # noqa: E712
                        )
                    )
                )
                .scalars()
                .all()
            )
            other_settings = (
                await db.execute(
                    select(UserSetting).where(
                        UserSetting.profile_id == other_profile_id
                    )
                )
            ).scalar_one()
            vendors = (
                (await db.execute(select(Vendor).order_by(Vendor.id.asc())))
                .scalars()
                .all()
            )

        assert len(target_endpoints) == 1
        assert target_endpoints[0].name == new_endpoint_name
        assert target_endpoints[0].name != old_target_endpoint_name

        assert len(target_models) == 1
        assert target_models[0].model_id == new_model_id
        assert target_models[0].model_id != old_target_model_id
        assert target_models[0].vendor_id == openrouter_vendor_id
        assert target_models[0].api_family == "openai"

        assert len(target_strategies) == 1
        assert target_strategies[0].strategy_type == "fill-first"
        assert target_strategies[0].failover_recovery_enabled is True
        assert target_strategies[0].failover_cooldown_seconds == 45
        assert target_strategies[0].failover_failure_threshold == 4
        assert target_strategies[0].failover_backoff_multiplier == 3.5
        assert target_strategies[0].failover_max_cooldown_seconds == 720
        assert target_strategies[0].failover_jitter_ratio == 0.35
        assert target_strategies[0].failover_status_codes == [
            403,
            422,
            429,
            500,
            502,
            503,
            504,
            529,
        ]
        assert target_strategies[0].failover_ban_mode == "temporary"
        assert target_strategies[0].failover_max_cooldown_strikes_before_ban == 2
        assert target_strategies[0].failover_ban_duration_seconds == 600

        assert len(target_connections) == 1
        assert target_connections[0].name == new_connection_name
        assert target_connections[0].qps_limit == 7
        assert target_connections[0].max_in_flight_non_stream == 9
        assert target_connections[0].max_in_flight_stream == 4

        assert len(target_fx_rows) == 1
        assert target_fx_rows[0].model_id == new_model_id
        assert target_fx_rows[0].endpoint_id == target_endpoints[0].id

        assert len(target_rules) == 1
        assert target_rules[0].pattern == new_rule_pattern
        assert target_rules[0].pattern != old_target_rule_pattern

        assert target_settings.report_currency_code == "GBP"
        assert target_settings.report_currency_symbol == "GBP"

        openrouter_rows = [vendor for vendor in vendors if vendor.key == "openrouter"]
        assert len(openrouter_rows) == 1
        assert openrouter_rows[0].id == openrouter_vendor_id
        assert openrouter_rows[0].name == "OpenRouter"
        assert openrouter_rows[0].icon_key == "openrouter"
        assert openrouter_rows[0].audit_enabled is True
        assert openrouter_rows[0].audit_capture_bodies is False

        assert len(other_endpoints) == 1
        assert other_endpoints[0].name == other_endpoint_name

        assert len(other_models) == 1
        assert other_models[0].model_id == other_model_id
        assert other_models[0].vendor_id == openai_vendor_id
        assert other_models[0].api_family == "openai"

        assert len(other_connections) == 1
        assert other_connections[0].name == other_connection_name
        assert other_connections[0].qps_limit is None
        assert other_connections[0].max_in_flight_non_stream is None
        assert other_connections[0].max_in_flight_stream is None

        assert len(other_fx_rows) == 1
        assert other_fx_rows[0].model_id == other_model_id

        assert len(other_rules) == 1
        assert other_rules[0].pattern == other_rule_pattern

        assert other_settings.report_currency_code == "EUR"
        assert other_settings.report_currency_symbol == "EUR"

    @pytest.mark.asyncio
    async def test_import_config_conflicting_global_vendor_key_fails_before_profile_replacement(
        self,
    ):
        from app.core.database import AsyncSessionLocal, get_engine
        from app.routers.config import import_config
        from app.schemas.schemas import ConfigImportRequest

        await get_engine().dispose()

        suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        conflict_vendor_key = f"zai-{suffix}"
        target_profile_name = f"import-conflict-target-{suffix}"
        old_target_endpoint_name = f"conflict-target-endpoint-old-{suffix}"
        old_target_model_id = f"conflict-target-model-old-{suffix}"
        old_target_connection_name = f"conflict-target-connection-old-{suffix}"
        old_target_rule_pattern = f"x-conflict-target-old-{suffix}"
        new_endpoint_name = f"conflict-target-endpoint-new-{suffix}"
        new_model_id = f"conflict-target-model-new-{suffix}"

        async with AsyncSessionLocal() as db:
            openai_vendor = (
                await db.execute(
                    select(Vendor)
                    .where(Vendor.key == "openai")
                    .order_by(Vendor.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if openai_vendor is None:
                openai_vendor = Vendor(
                    key="openai",
                    name="OpenAI",
                    description="OpenAI API (GPT models)",
                )
                db.add(openai_vendor)
                await db.flush()

            conflicting_vendor = Vendor(
                key=conflict_vendor_key,
                name=f"Legacy Z.ai {suffix}",
                description="Legacy vendor metadata",
                icon_key=None,
                audit_enabled=False,
                audit_capture_bodies=True,
            )
            db.add(conflicting_vendor)
            await db.flush()

            target_profile = Profile(
                name=target_profile_name,
                description="Target profile for conflict import",
                is_active=False,
                is_default=False,
                is_editable=True,
                version=0,
            )
            db.add(target_profile)
            await db.flush()

            target_endpoint = Endpoint(
                profile_id=target_profile.id,
                name=old_target_endpoint_name,
                base_url="https://api.openai.com",
                api_key="sk-target-old",
                position=0,
            )
            db.add(target_endpoint)
            await db.flush()

            target_model = ModelConfig(
                profile_id=target_profile.id,
                vendor_id=openai_vendor.id,
                api_family="openai",
                model_id=old_target_model_id,
                model_type="native",
                loadbalance_strategy=make_loadbalance_strategy(
                    profile_id=target_profile.id,
                    strategy_type="single",
                ),
                is_enabled=True,
            )
            db.add(target_model)
            await db.flush()

            db.add_all(
                [
                    Connection(
                        profile_id=target_profile.id,
                        model_config_id=target_model.id,
                        endpoint_id=target_endpoint.id,
                        is_active=True,
                        priority=0,
                        name=old_target_connection_name,
                    ),
                    UserSetting(
                        profile_id=target_profile.id,
                        report_currency_code="USD",
                        report_currency_symbol="$",
                    ),
                    HeaderBlocklistRule(
                        profile_id=target_profile.id,
                        name=f"conflict-target-rule-old-{suffix}",
                        match_type="exact",
                        pattern=old_target_rule_pattern,
                        enabled=True,
                        is_system=False,
                    ),
                ]
            )
            await db.commit()

            target_profile_id = target_profile.id

        payload = ConfigImportRequest.model_validate(
            {
                "version": 8,
                "vendors": [
                    {
                        "key": conflict_vendor_key,
                        "name": f"Z.ai {suffix}",
                        "description": "Z.ai Open Platform",
                        "icon_key": "zhipu",
                        "audit_enabled": False,
                        "audit_capture_bodies": True,
                    }
                ],
                "endpoints": [
                    {
                        "name": new_endpoint_name,
                        "base_url": "https://api.openai.com",
                        "api_key": "sk-target-new",
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
                        "vendor_key": conflict_vendor_key,
                        "api_family": "openai",
                        "model_id": new_model_id,
                        "model_type": "native",
                        "loadbalance_strategy_name": "single-primary",
                        "connections": [{"endpoint_name": new_endpoint_name}],
                    }
                ],
                "header_blocklist_rules": [],
            }
        )

        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await import_config(data=payload, db=db, profile_id=target_profile_id)
            await db.rollback()

        assert exc_info.value.status_code == 409
        assert conflict_vendor_key in str(exc_info.value.detail)

        async with AsyncSessionLocal() as db:
            persisted_vendor = (
                await db.execute(
                    select(Vendor).where(Vendor.key == conflict_vendor_key)
                )
            ).scalar_one()
            target_endpoints = (
                (
                    await db.execute(
                        select(Endpoint).where(Endpoint.profile_id == target_profile_id)
                    )
                )
                .scalars()
                .all()
            )
            target_models = (
                (
                    await db.execute(
                        select(ModelConfig).where(
                            ModelConfig.profile_id == target_profile_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            target_connections = (
                (
                    await db.execute(
                        select(Connection).where(
                            Connection.profile_id == target_profile_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            target_rules = (
                (
                    await db.execute(
                        select(HeaderBlocklistRule).where(
                            HeaderBlocklistRule.profile_id == target_profile_id,
                            HeaderBlocklistRule.is_system == False,  # noqa: E712
                        )
                    )
                )
                .scalars()
                .all()
            )

        assert persisted_vendor.name == f"Legacy Z.ai {suffix}"
        assert persisted_vendor.description == "Legacy vendor metadata"
        assert persisted_vendor.icon_key is None
        assert len(target_endpoints) == 1
        assert target_endpoints[0].name == old_target_endpoint_name
        assert len(target_models) == 1
        assert target_models[0].model_id == old_target_model_id
        assert len(target_connections) == 1
        assert target_connections[0].name == old_target_connection_name
        assert len(target_rules) == 1
        assert target_rules[0].pattern == old_target_rule_pattern
