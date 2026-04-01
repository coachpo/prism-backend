import asyncio
import json
import logging
from typing import AsyncGenerator, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tests.loadbalance_strategy_helpers import make_loadbalance_strategy
from fastapi import HTTPException

from app.services.proxy_service import (
    extract_model_from_body,
    build_upstream_headers,
)
from app.services.stats_service import log_request


def _make_vendor(*, key: str, name: str, description: str):
    from app.models.models import Vendor

    return Vendor(
        key=key,
        name=name,
        description=description,
    )


class TestDEF032_ProxyModelUpdateInvariants:
    @pytest.mark.asyncio
    async def test_create_proxy_model_allows_cross_vendor_redirect_when_api_family_matches(
        self,
    ):
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import ModelConfig, Profile
        from app.routers.models import create_model
        from app.schemas.schemas import ModelConfigCreate, ProxyTargetReference

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        target_a_model_id = f"def032-target-a-{suffix}"
        target_b_model_id = f"def032-target-b-{suffix}"
        proxy_model_id = f"def032-proxy-{suffix}"

        async with AsyncSessionLocal() as db:
            target_vendor = _make_vendor(
                key=f"def032-openai-target-{suffix}",
                name=f"DEF032 OpenAI Target {suffix}",
                description="DEF032 target vendor",
            )
            secondary_target_vendor = _make_vendor(
                key=f"def032-openai-secondary-{suffix}",
                name=f"DEF032 OpenAI Secondary {suffix}",
                description="DEF032 secondary target vendor",
            )
            proxy_vendor = _make_vendor(
                key=f"def032-openrouter-{suffix}",
                name=f"DEF032 OpenRouter {suffix}",
                description="DEF032 proxy vendor",
            )
            db.add_all([target_vendor, secondary_target_vendor, proxy_vendor])
            await db.flush()

            profile = Profile(
                name=f"DEF032 Create Profile {suffix}",
                is_active=False,
                version=0,
            )
            db.add(profile)
            await db.flush()

            target_a = ModelConfig(
                profile_id=profile.id,
                vendor_id=target_vendor.id,
                api_family="openai",
                model_id=target_a_model_id,
                model_type="native",
                loadbalance_strategy=make_loadbalance_strategy(
                    profile_id=profile.id,
                    strategy_type="single",
                ),
                is_enabled=True,
            )
            target_b = ModelConfig(
                profile_id=profile.id,
                vendor_id=secondary_target_vendor.id,
                api_family="openai",
                model_id=target_b_model_id,
                model_type="native",
                loadbalance_strategy=make_loadbalance_strategy(
                    profile_id=profile.id,
                    strategy_type="single",
                ),
                is_enabled=True,
            )
            db.add_all([target_a, target_b])
            await db.flush()

            response = await create_model(
                body=ModelConfigCreate(
                    vendor_id=proxy_vendor.id,
                    api_family="openai",
                    model_id=proxy_model_id,
                    model_type="proxy",
                    proxy_targets=[
                        ProxyTargetReference(
                            target_model_id=target_a_model_id,
                            position=0,
                        ),
                        ProxyTargetReference(
                            target_model_id=target_b_model_id,
                            position=1,
                        ),
                    ],
                    is_enabled=True,
                ),
                db=db,
                profile_id=profile.id,
            )
            await db.flush()

            proxy_model = (
                await db.execute(
                    select(ModelConfig).where(
                        ModelConfig.profile_id == profile.id,
                        ModelConfig.model_id == proxy_model_id,
                    )
                )
            ).scalar_one()

            assert response.model_type == "proxy"
            assert response.api_family == "openai"
            assert response.vendor_id == proxy_vendor.id
            assert [target.target_model_id for target in response.proxy_targets] == [
                target_a_model_id,
                target_b_model_id,
            ]
            assert proxy_model.vendor_id != target_a.vendor_id
            assert target_a.vendor_id != target_b.vendor_id
            assert proxy_model.id > 0

    @pytest.mark.asyncio
    async def test_create_proxy_model_rejects_redirect_when_api_family_differs_even_if_vendor_matches(
        self,
    ):
        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import ModelConfig, Profile
        from app.routers.models import create_model
        from app.schemas.schemas import ModelConfigCreate, ProxyTargetReference

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        native_model_id = f"def032-native-mismatch-{suffix}"
        proxy_model_id = f"def032-proxy-mismatch-{suffix}"

        async with AsyncSessionLocal() as db:
            shared_vendor = _make_vendor(
                key=f"def032-shared-vendor-{suffix}",
                name=f"DEF032 Shared Vendor {suffix}",
                description="DEF032 shared vendor",
            )
            profile = Profile(
                name=f"DEF032 Family Mismatch Profile {suffix}",
                is_active=False,
                version=0,
            )
            db.add_all([shared_vendor, profile])
            await db.flush()

            native_model = ModelConfig(
                profile_id=profile.id,
                vendor_id=shared_vendor.id,
                api_family="anthropic",
                model_id=native_model_id,
                model_type="native",
                loadbalance_strategy=make_loadbalance_strategy(
                    profile_id=profile.id,
                    strategy_type="single",
                ),
                is_enabled=True,
            )
            db.add(native_model)
            await db.flush()

            with pytest.raises(HTTPException) as exc_info:
                await create_model(
                    body=ModelConfigCreate(
                        vendor_id=shared_vendor.id,
                        api_family="openai",
                        model_id=proxy_model_id,
                        model_type="proxy",
                        proxy_targets=[
                            ProxyTargetReference(
                                target_model_id=native_model_id,
                                position=0,
                            )
                        ],
                        is_enabled=True,
                    ),
                    db=db,
                    profile_id=profile.id,
                )

            assert exc_info.value.status_code == 400
            assert "same api_family" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_update_model_renaming_native_keeps_proxy_target_resolution(self):
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import ModelConfig, ModelProxyTarget, Profile
        from app.routers.models import update_model
        from app.schemas.schemas import ModelConfigUpdate

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        native_model_id = f"def032-native-{suffix}"
        renamed_native_model_id = f"def032-native-renamed-{suffix}"
        proxy_model_id = f"def032-proxy-{suffix}"

        async with AsyncSessionLocal() as db:
            vendor = _make_vendor(
                key=f"def032-openai-rename-{suffix}",
                name=f"DEF032 OpenAI Rename {suffix}",
                description="DEF032 rename vendor",
            )
            db.add(vendor)
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
                vendor_id=vendor.id,
                api_family="openai",
                model_id=native_model_id,
                model_type="native",
                loadbalance_strategy=make_loadbalance_strategy(
                    profile_id=profile.id,
                    strategy_type="single",
                ),
                is_enabled=True,
            )
            proxy_model = ModelConfig(
                profile_id=profile.id,
                vendor_id=vendor.id,
                api_family="openai",
                model_id=proxy_model_id,
                model_type="proxy",
                is_enabled=True,
            )
            db.add_all([native_model, proxy_model])
            await db.flush()
            db.add(
                ModelProxyTarget(
                    source_model_config_id=proxy_model.id,
                    target_model_config_id=native_model.id,
                    position=0,
                )
            )
            await db.flush()

            response = await update_model(
                model_config_id=native_model.id,
                body=ModelConfigUpdate(model_id=renamed_native_model_id),
                db=db,
                profile_id=profile.id,
            )
            await db.flush()
            proxy_model = (
                await db.execute(
                    select(ModelConfig)
                    .options(
                        selectinload(ModelConfig.proxy_targets).selectinload(
                            ModelProxyTarget.target_model_config
                        )
                    )
                    .where(ModelConfig.id == proxy_model.id)
                )
            ).scalar_one()

            assert response.model_id == renamed_native_model_id
            assert (
                proxy_model.proxy_targets[0].target_model_id == renamed_native_model_id
            )

    @pytest.mark.asyncio
    async def test_update_native_model_rejects_api_family_change_while_proxy_models_point_to_it(
        self,
    ):
        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import ModelConfig, ModelProxyTarget, Profile
        from app.routers.models import update_model
        from app.schemas.schemas import ModelConfigUpdate

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        native_model_id = f"def032-native-family-{suffix}"
        proxy_model_id = f"def032-proxy-family-{suffix}"

        async with AsyncSessionLocal() as db:
            vendor = _make_vendor(
                key=f"def032-openai-family-{suffix}",
                name=f"DEF032 OpenAI Family {suffix}",
                description="DEF032 family vendor",
            )
            profile = Profile(
                name=f"DEF032 Family Guard Profile {suffix}",
                is_active=False,
                version=0,
            )
            db.add_all([vendor, profile])
            await db.flush()

            native_model = ModelConfig(
                profile_id=profile.id,
                vendor_id=vendor.id,
                api_family="openai",
                model_id=native_model_id,
                model_type="native",
                loadbalance_strategy=make_loadbalance_strategy(
                    profile_id=profile.id,
                    strategy_type="single",
                ),
                is_enabled=True,
            )
            proxy_model = ModelConfig(
                profile_id=profile.id,
                vendor_id=vendor.id,
                api_family="openai",
                model_id=proxy_model_id,
                model_type="proxy",
                is_enabled=True,
            )
            db.add_all([native_model, proxy_model])
            await db.flush()
            db.add(
                ModelProxyTarget(
                    source_model_config_id=proxy_model.id,
                    target_model_config_id=native_model.id,
                    position=0,
                )
            )
            await db.flush()

            with pytest.raises(HTTPException) as exc_info:
                await update_model(
                    model_config_id=native_model.id,
                    body=ModelConfigUpdate(api_family="anthropic"),
                    db=db,
                    profile_id=profile.id,
                )

            assert exc_info.value.status_code == 400
            assert "Cannot change api_family" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_update_model_rejects_converting_connected_native_model_to_proxy(
        self,
    ):
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import (
            Connection,
            Endpoint,
            ModelConfig,
            Profile,
        )
        from app.routers.models import update_model
        from app.schemas.schemas import ModelConfigUpdate, ProxyTargetReference

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        source_model_id = f"def032-source-{suffix}"
        target_model_id = f"def032-target-{suffix}"

        async with AsyncSessionLocal() as db:
            vendor = _make_vendor(
                key=f"def032-openai-connected-{suffix}",
                name=f"DEF032 OpenAI Connected {suffix}",
                description="DEF032 connected vendor",
            )
            db.add(vendor)
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
                vendor_id=vendor.id,
                api_family="openai",
                model_id=source_model_id,
                model_type="native",
                loadbalance_strategy=make_loadbalance_strategy(
                    profile_id=profile.id,
                    strategy_type="single",
                ),
                is_enabled=True,
            )
            target_model = ModelConfig(
                profile_id=profile.id,
                vendor_id=vendor.id,
                api_family="openai",
                model_id=target_model_id,
                model_type="native",
                loadbalance_strategy=make_loadbalance_strategy(
                    profile_id=profile.id,
                    strategy_type="single",
                ),
                is_enabled=True,
            )
            db.add_all([source_model, target_model])
            await db.flush()

            endpoint = Endpoint(
                profile_id=profile.id,
                name=f"DEF032 endpoint {suffix}",
                base_url="https://api.openai.com",
                api_key="sk-test",
                position=0,
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
                        proxy_targets=[
                            ProxyTargetReference(
                                target_model_id=target_model_id,
                                position=0,
                            )
                        ],
                    ),
                    db=db,
                    profile_id=profile.id,
                )

            assert exc_info.value.status_code == 400
            assert "Delete connections first" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_update_model_rejects_proxy_self_redirect(self):
        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import ModelConfig, Profile
        from app.routers.models import update_model
        from app.schemas.schemas import ModelConfigUpdate, ProxyTargetReference

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        model_id = f"def032-self-{suffix}"

        async with AsyncSessionLocal() as db:
            vendor = _make_vendor(
                key=f"def032-openai-self-{suffix}",
                name=f"DEF032 OpenAI Self {suffix}",
                description="DEF032 self vendor",
            )
            db.add(vendor)
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
            db.add(source_model)
            await db.flush()

            with pytest.raises(HTTPException) as exc_info:
                await update_model(
                    model_config_id=source_model.id,
                    body=ModelConfigUpdate(
                        model_type="proxy",
                        proxy_targets=[
                            ProxyTargetReference(
                                target_model_id=model_id,
                                position=0,
                            )
                        ],
                    ),
                    db=db,
                    profile_id=profile.id,
                )

            assert exc_info.value.status_code == 400
            assert exc_info.value.detail == "Proxy model cannot target itself"

    @pytest.mark.asyncio
    async def test_delete_native_model_rejects_attached_proxy_target(self):
        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import ModelConfig, ModelProxyTarget, Profile
        from app.routers.models import delete_model

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        native_model_id = f"def032-delete-native-{suffix}"
        proxy_model_id = f"def032-delete-proxy-{suffix}"

        async with AsyncSessionLocal() as db:
            vendor = _make_vendor(
                key=f"def032-openai-delete-{suffix}",
                name=f"DEF032 OpenAI Delete {suffix}",
                description="DEF032 delete vendor",
            )
            db.add(vendor)
            await db.flush()

            profile = Profile(
                name=f"DEF032 Delete Profile {suffix}",
                is_active=False,
                version=0,
            )
            db.add(profile)
            await db.flush()

            native_model = ModelConfig(
                profile_id=profile.id,
                vendor_id=vendor.id,
                api_family="openai",
                model_id=native_model_id,
                model_type="native",
                loadbalance_strategy=make_loadbalance_strategy(
                    profile_id=profile.id,
                    strategy_type="single",
                ),
                is_enabled=True,
            )
            proxy_model = ModelConfig(
                profile_id=profile.id,
                vendor_id=vendor.id,
                api_family="openai",
                model_id=proxy_model_id,
                model_type="proxy",
                is_enabled=True,
            )
            db.add_all([native_model, proxy_model])
            await db.flush()
            db.add(
                ModelProxyTarget(
                    source_model_config_id=proxy_model.id,
                    target_model_config_id=native_model.id,
                    position=0,
                )
            )
            await db.flush()

            with pytest.raises(HTTPException) as exc_info:
                await delete_model(
                    model_config_id=native_model.id,
                    db=db,
                    profile_id=profile.id,
                )

            assert exc_info.value.status_code == 400
            assert "point to this model" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_create_proxy_model_allows_empty_proxy_targets(self):
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import ModelConfig, ModelProxyTarget, Profile
        from app.routers.models import create_model
        from app.schemas.schemas import ModelConfigCreate

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        proxy_model_id = f"def032-empty-create-{suffix}"

        async with AsyncSessionLocal() as db:
            vendor = _make_vendor(
                key=f"def032-empty-create-{suffix}",
                name=f"DEF032 Empty Create {suffix}",
                description="DEF032 empty create vendor",
            )
            profile = Profile(
                name=f"DEF032 Empty Create Profile {suffix}",
                is_active=False,
                version=0,
            )
            db.add_all([vendor, profile])
            await db.flush()

            response = await create_model(
                body=ModelConfigCreate(
                    vendor_id=vendor.id,
                    api_family="openai",
                    model_id=proxy_model_id,
                    model_type="proxy",
                    proxy_targets=[],
                    is_enabled=True,
                ),
                db=db,
                profile_id=profile.id,
            )
            await db.flush()

            proxy_model = (
                await db.execute(
                    select(ModelConfig).where(
                        ModelConfig.profile_id == profile.id,
                        ModelConfig.model_id == proxy_model_id,
                    )
                )
            ).scalar_one()
            proxy_targets = (
                (
                    await db.execute(
                        select(ModelProxyTarget).where(
                            ModelProxyTarget.source_model_config_id == proxy_model.id
                        )
                    )
                )
                .scalars()
                .all()
            )

            assert response.model_type == "proxy"
            assert response.proxy_targets == []
            assert proxy_model.loadbalance_strategy_id is None
            assert proxy_targets == []

    @pytest.mark.asyncio
    async def test_update_proxy_model_allows_clearing_proxy_targets(self):
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import ModelConfig, ModelProxyTarget, Profile
        from app.routers.models import update_model
        from app.schemas.schemas import ModelConfigUpdate

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        target_model_id = f"def032-empty-update-target-{suffix}"
        proxy_model_id = f"def032-empty-update-proxy-{suffix}"

        async with AsyncSessionLocal() as db:
            vendor = _make_vendor(
                key=f"def032-empty-update-{suffix}",
                name=f"DEF032 Empty Update {suffix}",
                description="DEF032 empty update vendor",
            )
            profile = Profile(
                name=f"DEF032 Empty Update Profile {suffix}",
                is_active=False,
                version=0,
            )
            db.add_all([vendor, profile])
            await db.flush()

            target_model = ModelConfig(
                profile_id=profile.id,
                vendor_id=vendor.id,
                api_family="openai",
                model_id=target_model_id,
                model_type="native",
                loadbalance_strategy=make_loadbalance_strategy(
                    profile_id=profile.id,
                    strategy_type="single",
                ),
                is_enabled=True,
            )
            proxy_model = ModelConfig(
                profile_id=profile.id,
                vendor_id=vendor.id,
                api_family="openai",
                model_id=proxy_model_id,
                model_type="proxy",
                is_enabled=True,
            )
            db.add_all([target_model, proxy_model])
            await db.flush()
            db.add(
                ModelProxyTarget(
                    source_model_config_id=proxy_model.id,
                    target_model_config_id=target_model.id,
                    position=0,
                )
            )
            await db.flush()

            response = await update_model(
                model_config_id=proxy_model.id,
                body=ModelConfigUpdate(proxy_targets=[]),
                db=db,
                profile_id=profile.id,
            )
            await db.flush()

            proxy_targets = (
                (
                    await db.execute(
                        select(ModelProxyTarget).where(
                            ModelProxyTarget.source_model_config_id == proxy_model.id
                        )
                    )
                )
                .scalars()
                .all()
            )

            assert response.model_type == "proxy"
            assert response.proxy_targets == []
            assert proxy_targets == []

    def test_native_model_schema_still_rejects_non_empty_proxy_targets(self):
        from app.schemas.schemas import ModelConfigCreate, ProxyTargetReference

        with pytest.raises(ValidationError) as exc_info:
            ModelConfigCreate(
                vendor_id=1,
                api_family="openai",
                model_id="def032-native-invalid-proxy-targets",
                model_type="native",
                proxy_targets=[
                    ProxyTargetReference(
                        target_model_id="some-native-target",
                        position=0,
                    )
                ],
                loadbalance_strategy_id=1,
                is_enabled=True,
            )

        assert "proxy_targets must be empty for native models" in str(exc_info.value)

    def test_model_schema_normalizes_blank_display_name_inputs(self):
        from app.schemas.schemas import ModelConfigCreate, ModelConfigUpdate

        created = ModelConfigCreate(
            vendor_id=1,
            api_family="openai",
            model_id="def032-display-name-normalized-create",
            display_name="   ",
            model_type="native",
            proxy_targets=[],
            loadbalance_strategy_id=1,
            is_enabled=True,
        )
        updated = ModelConfigUpdate(display_name="   ")

        assert created.display_name is None
        assert updated.display_name is None

    def test_proxy_model_schema_rejects_duplicate_proxy_targets(self):
        from app.schemas.schemas import ModelConfigCreate, ProxyTargetReference

        with pytest.raises(ValidationError) as exc_info:
            ModelConfigCreate(
                vendor_id=1,
                api_family="openai",
                model_id="def032-duplicate-proxy-targets",
                model_type="proxy",
                proxy_targets=[
                    ProxyTargetReference(
                        target_model_id="native-target-a",
                        position=0,
                    ),
                    ProxyTargetReference(
                        target_model_id="native-target-a",
                        position=1,
                    ),
                ],
                is_enabled=True,
            )

        assert "proxy_targets must contain unique target_model_id values" in str(
            exc_info.value
        )

    def test_proxy_model_update_schema_rejects_duplicate_proxy_targets(self):
        from app.schemas.schemas import ModelConfigUpdate, ProxyTargetReference

        with pytest.raises(ValidationError) as exc_info:
            ModelConfigUpdate(
                proxy_targets=[
                    ProxyTargetReference(
                        target_model_id="native-target-a",
                        position=0,
                    ),
                    ProxyTargetReference(
                        target_model_id="native-target-a",
                        position=1,
                    ),
                ]
            )

        assert "proxy_targets must contain unique target_model_id values" in str(
            exc_info.value
        )

    def test_native_model_schema_still_requires_loadbalance_strategy(self):
        from app.schemas.schemas import ModelConfigCreate

        with pytest.raises(ValidationError) as exc_info:
            ModelConfigCreate(
                vendor_id=1,
                api_family="openai",
                model_id="def032-native-missing-strategy",
                model_type="native",
                proxy_targets=[],
                is_enabled=True,
            )

        assert "loadbalance_strategy_id is required for native models" in str(
            exc_info.value
        )

    @pytest.mark.asyncio
    async def test_create_and_update_model_normalize_blank_display_name_to_model_id(
        self,
    ):
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import ModelConfig, Profile
        from app.routers.models import create_model, update_model
        from app.schemas.schemas import ModelConfigCreate, ModelConfigUpdate

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        model_id = f"def032-display-name-normalized-{suffix}"

        async with AsyncSessionLocal() as db:
            vendor = _make_vendor(
                key=f"def032-display-name-vendor-{suffix}",
                name=f"DEF032 Display Name Vendor {suffix}",
                description="DEF032 display name vendor",
            )
            profile = Profile(
                name=f"DEF032 Display Name Profile {suffix}",
                is_active=False,
                version=0,
            )
            db.add_all([vendor, profile])
            await db.flush()

            strategy = make_loadbalance_strategy(
                profile_id=profile.id,
                strategy_type="single",
            )
            db.add(strategy)
            await db.flush()

            created = await create_model(
                body=ModelConfigCreate(
                    vendor_id=vendor.id,
                    api_family="openai",
                    model_id=model_id,
                    model_type="native",
                    proxy_targets=[],
                    loadbalance_strategy_id=strategy.id,
                    is_enabled=True,
                ),
                db=db,
                profile_id=profile.id,
            )
            await db.flush()

            stored_after_create = (
                await db.execute(
                    select(ModelConfig).where(
                        ModelConfig.profile_id == profile.id,
                        ModelConfig.model_id == model_id,
                    )
                )
            ).scalar_one()

            assert created.display_name == model_id
            assert stored_after_create.display_name == model_id

            updated_custom = await update_model(
                model_config_id=stored_after_create.id,
                body=ModelConfigUpdate(display_name="Friendly Display"),
                db=db,
                profile_id=profile.id,
            )
            await db.flush()

            assert updated_custom.display_name == "Friendly Display"

            updated_blank = await update_model(
                model_config_id=stored_after_create.id,
                body=ModelConfigUpdate(display_name="   "),
                db=db,
                profile_id=profile.id,
            )
            await db.flush()

            stored_after_blank_update = (
                await db.execute(
                    select(ModelConfig).where(ModelConfig.id == stored_after_create.id)
                )
            ).scalar_one()

            assert updated_blank.display_name == model_id
            assert stored_after_blank_update.display_name == model_id
