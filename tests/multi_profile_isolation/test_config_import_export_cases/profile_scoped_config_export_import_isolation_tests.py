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
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            position=0,
        )
        model = SimpleNamespace(
            id=20,
            profile_id=1,
            provider_id=1,
            model_id="gpt-4",
            display_name=None,
            model_type="native",
            redirect_to=None,
            loadbalance_strategy_id=11,
            loadbalance_strategy=SimpleNamespace(
                id=11,
                name="single-primary",
                strategy_type="single",
                failover_recovery_enabled=False,
            ),
            is_enabled=True,
            connections=[],
            created_at=now,
            updated_at=now,
        )

        # Mock query results
        endpoint_result = MagicMock()
        endpoint_result.scalars.return_value.all.return_value = [endpoint]

        model_result = MagicMock()
        model_result.scalars.return_value.all.return_value = [model]

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

        providers_result = MagicMock()
        providers_result.scalars.return_value.all.return_value = []

        mock_db.execute.side_effect = [
            endpoint_result,
            model_result,
            strategies_result,
            pricing_templates_result,
            providers_result,
            user_settings_result,
            fx_result,
            blocklist_result,
        ]

        config = await export_config(db=mock_db, profile_id=1)
        payload = json.loads(bytes(config.body).decode("utf-8"))

        # Verify export contains profile 1 data only
        assert len(payload["endpoints"]) == 1
        assert len(payload["models"]) == 1
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
            provider = (
                await db.execute(
                    select(Provider)
                    .where(Provider.provider_type == "openai")
                    .order_by(Provider.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if provider is None:
                provider = Provider(
                    name=f"OpenAI {suffix}",
                    provider_type="openai",
                )
                db.add(provider)
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
                base_url="https://api.openai.com/v1",
                api_key="sk-target-old",
                position=0,
            )
            other_endpoint = Endpoint(
                profile_id=other_profile.id,
                name=other_endpoint_name,
                base_url="https://api.openai.com/v1",
                api_key="sk-other",
                position=0,
            )
            db.add_all([target_endpoint, other_endpoint])
            await db.flush()

            target_model = ModelConfig(
                profile_id=target_profile.id,
                provider_id=provider.id,
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
                provider_id=provider.id,
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

        payload = ConfigImportRequest.model_validate(
            {
                "version": 3,
                "endpoints": [
                    {
                        "name": new_endpoint_name,
                        "base_url": "https://api.openai.com/v1",
                        "api_key": "sk-target-new",
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
                        "model_id": new_model_id,
                        "model_type": "native",
                        "loadbalance_strategy_name": "single-primary",
                        "connections": [
                            {
                                "endpoint_name": new_endpoint_name,
                                "name": new_connection_name,
                                "priority": 0,
                                "is_active": True,
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

        assert len(target_endpoints) == 1
        assert target_endpoints[0].name == new_endpoint_name
        assert target_endpoints[0].name != old_target_endpoint_name

        assert len(target_models) == 1
        assert target_models[0].model_id == new_model_id
        assert target_models[0].model_id != old_target_model_id

        assert len(target_connections) == 1
        assert target_connections[0].name == new_connection_name

        assert len(target_fx_rows) == 1
        assert target_fx_rows[0].model_id == new_model_id
        assert target_fx_rows[0].endpoint_id == target_endpoints[0].id

        assert len(target_rules) == 1
        assert target_rules[0].pattern == new_rule_pattern
        assert target_rules[0].pattern != old_target_rule_pattern

        assert target_settings.report_currency_code == "GBP"
        assert target_settings.report_currency_symbol == "GBP"

        assert len(other_endpoints) == 1
        assert other_endpoints[0].name == other_endpoint_name

        assert len(other_models) == 1
        assert other_models[0].model_id == other_model_id

        assert len(other_connections) == 1
        assert other_connections[0].name == other_connection_name

        assert len(other_fx_rows) == 1
        assert other_fx_rows[0].model_id == other_model_id

        assert len(other_rules) == 1
        assert other_rules[0].pattern == other_rule_pattern

        assert other_settings.report_currency_code == "EUR"
        assert other_settings.report_currency_symbol == "EUR"
