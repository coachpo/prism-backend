import asyncio
import json
import logging
from typing import AsyncGenerator, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.loadbalance_strategy_helpers import make_loadbalance_strategy
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
            route.path: route
            for route in proxy_router.router.routes
            if hasattr(route, "dependant")
        }

        v1_route = route_by_path["/v1/{path:path}"]
        v1beta_route = route_by_path["/v1beta/{path:path}"]

        v1_dependencies = {dep.call for dep in v1_route.dependant.dependencies}
        v1beta_dependencies = {dep.call for dep in v1beta_route.dependant.dependencies}

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
            Provider,
        )
        from app.routers.models import get_model
        from app.schemas.schemas import ModelConfigResponse

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        model_id = f"def065-model-{suffix}"

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
                    name=f"DEF065 OpenAI {suffix}",
                    provider_type="openai",
                    description="DEF065 provider",
                )
                db.add(provider)
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
                provider_id=provider.id,
                model_id=model_id,
                model_type="native",
                redirect_to=None,
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
                base_url="https://api.openai.com/v1",
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
