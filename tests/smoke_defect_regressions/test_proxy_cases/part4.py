import asyncio
import json
import logging
from typing import AsyncGenerator, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.proxy_service import (
    extract_model_from_body,
    build_upstream_headers,
)
from app.services.stats_service import log_request

class TestDEF032_ProxyModelUpdateInvariants:
    @pytest.mark.asyncio
    async def test_update_model_renaming_native_cascades_proxy_redirects(self):
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import ModelConfig, Profile, Provider
        from app.routers.models import update_model
        from app.schemas.schemas import ModelConfigUpdate

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        native_model_id = f"def032-native-{suffix}"
        renamed_native_model_id = f"def032-native-renamed-{suffix}"
        proxy_model_id = f"def032-proxy-{suffix}"

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
                    name=f"DEF032 OpenAI {suffix}",
                    provider_type="openai",
                    description="DEF032 provider",
                )
                db.add(provider)
                await db.flush()

            profile = Profile(
                name=f"DEF032 Profile {suffix}",
                is_active=False,
                version=0,
            )
            db.add(profile)
            await db.flush()

            native_model = ModelConfig(
                profile_id=profile.id,
                provider_id=provider.id,
                model_id=native_model_id,
                model_type="native",
                redirect_to=None,
                lb_strategy="single",
                failover_recovery_enabled=True,
                failover_recovery_cooldown_seconds=60,
                is_enabled=True,
            )
            proxy_model = ModelConfig(
                profile_id=profile.id,
                provider_id=provider.id,
                model_id=proxy_model_id,
                model_type="proxy",
                redirect_to=native_model_id,
                lb_strategy="single",
                failover_recovery_enabled=True,
                failover_recovery_cooldown_seconds=60,
                is_enabled=True,
            )
            db.add_all([native_model, proxy_model])
            await db.flush()

            response = await update_model(
                model_config_id=native_model.id,
                body=ModelConfigUpdate(model_id=renamed_native_model_id),
                db=db,
                profile_id=profile.id,
            )
            await db.flush()
            await db.refresh(proxy_model)

            assert response.model_id == renamed_native_model_id
            assert proxy_model.redirect_to == renamed_native_model_id

    @pytest.mark.asyncio
    async def test_update_model_rejects_converting_connected_native_model_to_proxy(self):
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import Connection, Endpoint, ModelConfig, Profile, Provider
        from app.routers.models import update_model
        from app.schemas.schemas import ModelConfigUpdate

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        source_model_id = f"def032-source-{suffix}"
        target_model_id = f"def032-target-{suffix}"

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
                    name=f"DEF032 OpenAI {suffix}",
                    provider_type="openai",
                    description="DEF032 provider",
                )
                db.add(provider)
                await db.flush()

            profile = Profile(
                name=f"DEF032 Profile Connected {suffix}",
                is_active=False,
                version=0,
            )
            db.add(profile)
            await db.flush()

            source_model = ModelConfig(
                profile_id=profile.id,
                provider_id=provider.id,
                model_id=source_model_id,
                model_type="native",
                redirect_to=None,
                lb_strategy="single",
                failover_recovery_enabled=True,
                failover_recovery_cooldown_seconds=60,
                is_enabled=True,
            )
            target_model = ModelConfig(
                profile_id=profile.id,
                provider_id=provider.id,
                model_id=target_model_id,
                model_type="native",
                redirect_to=None,
                lb_strategy="single",
                failover_recovery_enabled=True,
                failover_recovery_cooldown_seconds=60,
                is_enabled=True,
            )
            db.add_all([source_model, target_model])
            await db.flush()

            endpoint = Endpoint(
                profile_id=profile.id,
                name=f"DEF032 endpoint {suffix}",
                base_url="https://api.openai.com/v1",
                api_key="sk-test",
            )
            db.add(endpoint)
            await db.flush()

            db.add(
                Connection(
                    profile_id=profile.id,
                    model_config_id=source_model.id,
                    endpoint_id=endpoint.id,
                    is_active=True,
                    priority=0,
                )
            )
            await db.flush()

            with pytest.raises(HTTPException) as exc_info:
                await update_model(
                    model_config_id=source_model.id,
                    body=ModelConfigUpdate(
                        model_type="proxy",
                        redirect_to=target_model_id,
                    ),
                    db=db,
                    profile_id=profile.id,
                )

            assert exc_info.value.status_code == 400
            assert "Delete connections first" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_update_model_rejects_proxy_self_redirect(self):
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import ModelConfig, Profile, Provider
        from app.routers.models import update_model
        from app.schemas.schemas import ModelConfigUpdate

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        model_id = f"def032-self-{suffix}"

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
                    name=f"DEF032 OpenAI {suffix}",
                    provider_type="openai",
                    description="DEF032 provider",
                )
                db.add(provider)
                await db.flush()

            profile = Profile(
                name=f"DEF032 Profile Self {suffix}",
                is_active=False,
                version=0,
            )
            db.add(profile)
            await db.flush()

            source_model = ModelConfig(
                profile_id=profile.id,
                provider_id=provider.id,
                model_id=model_id,
                model_type="native",
                redirect_to=None,
                lb_strategy="single",
                failover_recovery_enabled=True,
                failover_recovery_cooldown_seconds=60,
                is_enabled=True,
            )
            db.add(source_model)
            await db.flush()

            with pytest.raises(HTTPException) as exc_info:
                await update_model(
                    model_config_id=source_model.id,
                    body=ModelConfigUpdate(
                        model_type="proxy",
                        redirect_to=model_id,
                    ),
                    db=db,
                    profile_id=profile.id,
                )

            assert exc_info.value.status_code == 400
            assert exc_info.value.detail == "Proxy model cannot redirect to itself"

