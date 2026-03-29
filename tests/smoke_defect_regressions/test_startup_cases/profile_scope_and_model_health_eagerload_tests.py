import asyncio
import json
import logging
from typing import AsyncGenerator, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.loadbalance_strategy_helpers import (
    DEFAULT_FAILOVER_STATUS_CODES,
    make_auto_recovery_enabled,
    make_loadbalance_strategy,
)
from fastapi import HTTPException

from app.services.proxy_service import (
    extract_model_from_body,
    build_upstream_headers,
)
from app.services.stats_service import log_request


class TestDEF022_ProfileIsolationRuntimeDependencies:
    def test_proxy_routes_use_active_profile_dependency(self):
        from app.dependencies import get_active_profile_id, get_effective_profile_id
        from app.routers import proxy as proxy_router

        route_by_path = {
            cast(str, getattr(route, "path")): route
            for route in proxy_router.router.routes
            if getattr(route, "dependant", None) is not None
        }

        v1_route = route_by_path["/v1/{path:path}"]
        v1beta_route = route_by_path["/v1beta/{path:path}"]

        v1_dependant = cast(object, getattr(v1_route, "dependant"))
        v1beta_dependant = cast(object, getattr(v1beta_route, "dependant"))
        v1_dependencies = {
            getattr(dep, "call")
            for dep in cast(list[object], getattr(v1_dependant, "dependencies"))
        }
        v1beta_dependencies = {
            getattr(dep, "call")
            for dep in cast(list[object], getattr(v1beta_dependant, "dependencies"))
        }

        assert get_active_profile_id in v1_dependencies
        assert get_active_profile_id in v1beta_dependencies
        assert get_effective_profile_id not in v1_dependencies
        assert get_effective_profile_id not in v1beta_dependencies


class TestDEF065_ModelDetailEndpointEagerLoad:
    """DEF-065 (P1): model detail responses must eagerly load connection endpoints."""

    @pytest.mark.asyncio
    async def test_get_model_returns_connections_with_endpoint_loaded(self):
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import (
            Connection,
            Endpoint,
            ModelConfig,
            Profile,
            Vendor,
        )
        from app.routers.models import get_model
        from app.schemas.domains.connection_model import AutoRecoveryEnabled
        from app.schemas.schemas import ModelConfigResponse

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        model_id = f"def065-model-{suffix}"

        async with AsyncSessionLocal() as db:
            vendor = (
                await db.execute(
                    select(Vendor)
                    .where(Vendor.key == "openai")
                    .order_by(Vendor.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if vendor is None:
                vendor = Vendor(
                    key="openai",
                    name=f"DEF065 OpenAI {suffix}",
                    description="DEF065 provider",
                )
                db.add(vendor)
                await db.flush()

            profile = Profile(
                name=f"DEF065 Profile {suffix}",
                is_active=False,
                version=0,
            )
            db.add(profile)
            await db.flush()

            model = ModelConfig(
                profile_id=profile.id,
                vendor_id=vendor.id,
                api_family="openai",
                model_id=model_id,
                model_type="native",
                loadbalance_strategy=make_loadbalance_strategy(
                    profile_id=profile.id,
                    strategy_type="single",
                ),
                is_enabled=True,
            )
            db.add(model)
            await db.flush()

            endpoint = Endpoint(
                profile_id=profile.id,
                name=f"DEF065 endpoint {suffix}",
                base_url="https://api.openai.com",
                api_key="sk-test",
                position=0,
            )
            db.add(endpoint)
            await db.flush()

            connection = Connection(
                profile_id=profile.id,
                model_config_id=model.id,
                endpoint_id=endpoint.id,
                is_active=True,
                priority=0,
                name=f"DEF065 connection {suffix}",
            )
            db.add(connection)
            await db.flush()

            config = await get_model(
                model_config_id=model.id,
                db=db,
                profile_id=profile.id,
            )
            response = ModelConfigResponse.model_validate(config, from_attributes=True)

            assert len(response.connections) == 1
            assert response.connections[0].id == connection.id
            assert response.connections[0].endpoint is not None
            assert response.connections[0].endpoint.id == endpoint.id

    @pytest.mark.asyncio
    async def test_model_routes_resolve_effective_policy_for_legacy_strategy_rows(self):
        from sqlalchemy import select

        from app.core.config import get_settings
        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import ModelConfig, Profile, Vendor
        from app.routers.models import get_model, list_models
        from app.schemas.domains.connection_model import AutoRecoveryEnabled
        from app.schemas.schemas import ModelConfigResponse

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        model_id = f"def065-legacy-policy-{suffix}"
        settings = get_settings()

        async with AsyncSessionLocal() as db:
            vendor = (
                await db.execute(
                    select(Vendor)
                    .where(Vendor.key == "openai")
                    .order_by(Vendor.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if vendor is None:
                vendor = Vendor(
                    key="openai",
                    name=f"DEF065 Legacy OpenAI {suffix}",
                    description="DEF065 legacy provider",
                )
                db.add(vendor)
                await db.flush()

            profile = Profile(
                name=f"DEF065 Legacy Profile {suffix}",
                is_active=False,
                version=0,
            )
            db.add(profile)
            await db.flush()

            strategy = make_loadbalance_strategy(
                profile_id=profile.id,
                strategy_type="failover",
                auto_recovery=make_auto_recovery_enabled(
                    status_codes=DEFAULT_FAILOVER_STATUS_CODES,
                    base_seconds=settings.failover_cooldown_seconds,
                    failure_threshold=settings.failover_failure_threshold,
                    backoff_multiplier=settings.failover_backoff_multiplier,
                    max_cooldown_seconds=settings.failover_max_cooldown_seconds,
                    jitter_ratio=settings.failover_jitter_ratio,
                ),
            )

            model = ModelConfig(
                profile_id=profile.id,
                vendor_id=vendor.id,
                api_family="openai",
                model_id=model_id,
                model_type="native",
                loadbalance_strategy=strategy,
                is_enabled=True,
            )
            db.add_all([strategy, model])
            await db.flush()

            config = await get_model(
                model_config_id=model.id,
                db=db,
                profile_id=profile.id,
            )
            response = ModelConfigResponse.model_validate(config, from_attributes=True)

            assert response.loadbalance_strategy is not None
            assert response.loadbalance_strategy.strategy_type == "failover"
            response_auto_recovery = response.loadbalance_strategy.auto_recovery
            assert isinstance(response_auto_recovery, AutoRecoveryEnabled)
            assert (
                response_auto_recovery.cooldown.base_seconds
                == settings.failover_cooldown_seconds
            )
            assert (
                response_auto_recovery.cooldown.failure_threshold
                == settings.failover_failure_threshold
            )
            assert response_auto_recovery.cooldown.backoff_multiplier == pytest.approx(
                settings.failover_backoff_multiplier
            )
            assert (
                response_auto_recovery.cooldown.max_cooldown_seconds
                == settings.failover_max_cooldown_seconds
            )
            assert response_auto_recovery.cooldown.jitter_ratio == pytest.approx(
                settings.failover_jitter_ratio
            )
            assert response_auto_recovery.status_codes == [
                403,
                422,
                429,
                500,
                502,
                503,
                504,
                529,
            ]

            with patch(
                "app.routers.models.get_model_health_stats",
                AsyncMock(return_value={}),
            ):
                listed = await list_models(db=db, profile_id=profile.id)

            assert len(listed) == 1
            assert listed[0].loadbalance_strategy is not None
            listed_auto_recovery = listed[0].loadbalance_strategy.auto_recovery
            assert isinstance(listed_auto_recovery, AutoRecoveryEnabled)
            assert (
                listed_auto_recovery.cooldown.base_seconds
                == settings.failover_cooldown_seconds
            )
            assert (
                listed_auto_recovery.cooldown.failure_threshold
                == settings.failover_failure_threshold
            )


class TestDEF025_ModelHealthStatsProfileScope:
    @pytest.mark.asyncio
    async def test_list_models_passes_profile_id_to_health_stats(self):
        from app.routers.models import list_models

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch(
            "app.routers.models.get_model_health_stats",
            AsyncMock(return_value={}),
        ) as health_mock:
            response = await list_models(db=mock_db, profile_id=7)

        assert response == []
        health_mock.assert_awaited_once_with(mock_db, profile_id=7)

    @pytest.mark.asyncio
    async def test_get_models_by_endpoint_passes_profile_id_to_health_stats(self):
        from app.routers.models import get_models_by_endpoint

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch(
            "app.routers.models.get_model_health_stats",
            AsyncMock(return_value={}),
        ) as health_mock:
            response = await get_models_by_endpoint(
                endpoint_id=123,
                db=mock_db,
                profile_id=9,
            )

        assert response == []
        health_mock.assert_awaited_once_with(mock_db, profile_id=9)
