from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError
from unittest.mock import AsyncMock

from tests.loadbalance_strategy_helpers import make_loadbalance_strategy
from tests.smoke_defect_regressions.test_startup_cases.auth_management_flows_tests import (
    _cleanup_auth_state,
    _login,
    _reset_auth_state,
)

from app.core.database import AsyncSessionLocal, get_engine
from app.main import app
from app.models.models import ModelConfig, Profile, Vendor
from app.routers.vendors import delete_vendor


async def _create_profile(session, *, name: str) -> Profile:
    profile = Profile(name=name, is_active=False, version=0)
    session.add(profile)
    await session.flush()
    return profile


async def _create_vendor(session, *, suffix: str, label: str) -> Vendor:
    vendor = Vendor(
        key=f"def084-{label}-{suffix}",
        name=f"DEF084 {label} {suffix}",
    )
    session.add(vendor)
    await session.flush()
    return vendor


async def _create_model(
    session,
    *,
    profile_id: int,
    vendor_id: int,
    model_id: str,
    display_name: str,
    api_family: str = "openai",
    model_type: str = "native",
    is_enabled: bool = True,
) -> ModelConfig:
    loadbalance_strategy = None
    if model_type == "native":
        loadbalance_strategy = make_loadbalance_strategy(
            profile_id=profile_id,
            strategy_type="single",
        )

    model = ModelConfig(
        profile_id=profile_id,
        vendor_id=vendor_id,
        api_family=api_family,
        model_id=model_id,
        display_name=display_name,
        model_type=model_type,
        loadbalance_strategy=loadbalance_strategy,
        is_enabled=is_enabled,
    )
    session.add(model)
    await session.flush()
    return model


class TestDEF084_VendorDeleteSafety:
    @pytest.mark.asyncio
    async def test_vendor_routes_round_trip_icon_key_and_normalize_it(self):
        await get_engine().dispose()
        await _reset_auth_state()
        transport = ASGITransport(app=app)
        suffix = uuid4().hex[:8]

        try:
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                await _login(client)

                create_response = await client.post(
                    "/api/vendors",
                    json={
                        "key": f"zai-{suffix}",
                        "name": f"Z.ai {suffix}",
                        "description": "Z.ai Open Platform",
                        "icon_key": "  ZHIPU  ",
                    },
                )

                assert create_response.status_code == 201
                created_vendor = create_response.json()
                assert created_vendor["icon_key"] == "zhipu"

                vendor_id = created_vendor["id"]

                update_response = await client.patch(
                    f"/api/vendors/{vendor_id}",
                    json={
                        "description": " Updated description ",
                        "icon_key": "   ",
                    },
                )

                assert update_response.status_code == 200
                updated_vendor = update_response.json()
                assert updated_vendor["description"] == "Updated description"
                assert updated_vendor["icon_key"] is None

                get_response = await client.get(f"/api/vendors/{vendor_id}")
                assert get_response.status_code == 200
                assert get_response.json()["icon_key"] is None

                list_response = await client.get("/api/vendors")
                assert list_response.status_code == 200
                listed_vendor = next(
                    vendor
                    for vendor in list_response.json()
                    if vendor["id"] == vendor_id
                )
                assert listed_vendor["icon_key"] is None
        finally:
            await _cleanup_auth_state()

    @pytest.mark.asyncio
    async def test_unused_vendor_delete_succeeds_with_204(self):
        await get_engine().dispose()
        await _reset_auth_state()
        transport = ASGITransport(app=app)
        suffix = uuid4().hex[:8]

        try:
            async with AsyncSessionLocal() as session:
                vendor = await _create_vendor(
                    session,
                    suffix=suffix,
                    label="unused-vendor",
                )
                vendor_id = vendor.id
                await session.commit()

            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                await _login(client)
                response = await client.delete(f"/api/vendors/{vendor_id}")

            assert response.status_code == 204
            assert response.content == b""

            async with AsyncSessionLocal() as session:
                deleted_vendor = await session.get(Vendor, vendor_id)
                assert deleted_vendor is None
        finally:
            await _cleanup_auth_state()

    @pytest.mark.asyncio
    async def test_delete_vendor_in_use_returns_409_with_model_rows(self):
        await get_engine().dispose()
        await _reset_auth_state()
        transport = ASGITransport(app=app)
        suffix = uuid4().hex[:8]

        try:
            async with AsyncSessionLocal() as session:
                vendor = await _create_vendor(
                    session,
                    suffix=suffix,
                    label="referenced-vendor",
                )
                profile = await _create_profile(
                    session,
                    name=f"DEF084 Referenced Profile {suffix}",
                )
                model = await _create_model(
                    session,
                    profile_id=profile.id,
                    vendor_id=vendor.id,
                    model_id=f"def083-referenced-model-{suffix}",
                    display_name=f"DEF084 Referenced Model {suffix}",
                )
                await session.commit()

            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                await _login(client)
                response = await client.delete(f"/api/vendors/{vendor.id}")

            assert response.status_code == 409
            assert response.json() == {
                "detail": {
                    "message": "Cannot delete vendor that is referenced by models",
                    "models": [
                        {
                            "model_config_id": model.id,
                            "profile_id": profile.id,
                            "profile_name": profile.name,
                            "model_id": model.model_id,
                            "display_name": model.display_name,
                            "model_type": model.model_type,
                            "api_family": model.api_family,
                            "is_enabled": model.is_enabled,
                        }
                    ],
                }
            }
        finally:
            await _cleanup_auth_state()

    @pytest.mark.asyncio
    async def test_get_vendor_models_returns_rows_with_profile_context(self):
        await get_engine().dispose()
        await _reset_auth_state()
        transport = ASGITransport(app=app)
        suffix = uuid4().hex[:8]

        try:
            async with AsyncSessionLocal() as session:
                vendor = await _create_vendor(
                    session,
                    suffix=suffix,
                    label="usage-vendor",
                )
                profile = await _create_profile(
                    session,
                    name=f"DEF084 Usage Alpha {suffix}",
                )
                model = await _create_model(
                    session,
                    profile_id=profile.id,
                    vendor_id=vendor.id,
                    model_id=f"def083-usage-model-{suffix}",
                    display_name=f"DEF084 Usage Model {suffix}",
                    api_family="anthropic",
                    is_enabled=False,
                )
                await session.commit()

            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                await _login(client)
                response = await client.get(f"/api/vendors/{vendor.id}/models")

            assert response.status_code == 200
            assert response.json() == [
                {
                    "model_config_id": model.id,
                    "profile_id": profile.id,
                    "profile_name": profile.name,
                    "model_id": model.model_id,
                    "display_name": model.display_name,
                    "model_type": model.model_type,
                    "api_family": model.api_family,
                    "is_enabled": model.is_enabled,
                }
            ]
        finally:
            await _cleanup_auth_state()

    def test_vendor_model_configs_relationship_excludes_delete_cascades(self):
        cascade = Vendor.model_configs.property.cascade

        assert "delete" not in cascade
        assert "delete-orphan" not in cascade

    @pytest.mark.asyncio
    async def test_delete_vendor_translates_commit_time_integrity_error_to_409(self):
        await get_engine().dispose()
        suffix = uuid4().hex[:8]

        async with AsyncSessionLocal() as session:
            vendor = await _create_vendor(
                session,
                suffix=suffix,
                label="commit-failure-vendor",
            )
            vendor_id = vendor.id
            await session.commit()

        async with AsyncSessionLocal() as session:
            setattr(
                session,
                "commit",
                AsyncMock(
                    side_effect=IntegrityError(
                        "DELETE FROM vendors", {}, Exception("fk")
                    )
                ),
            )

            try:
                with pytest.raises(HTTPException) as exc_info:
                    await delete_vendor(vendor_id=vendor_id, db=session)
            finally:
                await session.rollback()

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == {
            "message": "Cannot delete vendor that is referenced by models",
            "models": [],
        }
