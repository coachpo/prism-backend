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


class TestDEF024_ConfigImportExportRefRoundtrip:
    @pytest.mark.asyncio
    async def test_create_and_update_connection_preserve_limiter_fields(self):
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import Endpoint, ModelConfig, Profile, Vendor
        from app.routers.connections import create_connection, update_connection
        from app.schemas.schemas import ConnectionCreate, ConnectionUpdate
        from tests.loadbalance_strategy_helpers import make_loadbalance_strategy

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))

        async with AsyncSessionLocal() as db:
            profile = Profile(
                name=f"DEF024 limiter profile {suffix}",
                is_active=False,
                is_default=False,
                is_editable=True,
                version=0,
            )
            db.add(profile)
            await db.flush()

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
                    name=f"DEF024 limiter OpenAI {suffix}",
                )
                db.add(vendor)
                await db.flush()

            endpoint = Endpoint(
                profile_id=profile.id,
                name=f"def024-limiter-endpoint-{suffix}",
                base_url="https://api.openai.com",
                api_key="sk-limiter-test",
                position=0,
            )
            model = ModelConfig(
                profile_id=profile.id,
                vendor_id=vendor.id,
                api_family="openai",
                model_id=f"def024-limiter-model-{suffix}",
                model_type="native",
                loadbalance_strategy=make_loadbalance_strategy(
                    profile_id=profile.id,
                    strategy_type="single",
                ),
                is_enabled=True,
            )
            db.add_all([endpoint, model])
            await db.flush()

            created = await create_connection(
                model_config_id=model.id,
                body=ConnectionCreate(
                    endpoint_id=endpoint.id,
                    name=f"def024-limiter-connection-{suffix}",
                    qps_limit=3,
                    max_in_flight_non_stream=5,
                    max_in_flight_stream=2,
                ),
                db=db,
                profile_id=profile.id,
            )

            assert created.qps_limit == 3
            assert created.max_in_flight_non_stream == 5
            assert created.max_in_flight_stream == 2

            updated = await update_connection(
                connection_id=created.id,
                body=ConnectionUpdate(
                    qps_limit=4,
                    max_in_flight_non_stream=None,
                    max_in_flight_stream=6,
                ),
                db=db,
                profile_id=profile.id,
            )

            assert updated.qps_limit == 4
            assert updated.max_in_flight_non_stream is None
            assert updated.max_in_flight_stream == 6

    @pytest.mark.asyncio
    async def test_import_export_roundtrip_omits_id_fields(self):
        from sqlalchemy import select
        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import (
            Connection,
            Endpoint,
            EndpointFxRateSetting,
            Profile,
            Vendor,
        )
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
                "version": 6,
                "vendors": [
                    {
                        "key": "openai",
                        "name": "OpenAI",
                        "description": None,
                        "audit_enabled": False,
                        "audit_capture_bodies": True,
                    }
                ],
                "endpoints": [
                    {
                        "name": endpoint_name,
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
                        "vendor_key": "openai",
                        "api_family": "openai",
                        "model_id": model_id,
                        "model_type": "native",
                        "loadbalance_strategy_name": "single-primary",
                        "connections": [
                            {
                                "endpoint_name": endpoint_name,
                                "name": connection_name,
                                "qps_limit": 3,
                                "max_in_flight_non_stream": 5,
                                "max_in_flight_stream": 2,
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
                            "endpoint_name": endpoint_name,
                            "fx_rate": "1.25",
                        }
                    ],
                },
            }
        )

        async with AsyncSessionLocal() as db:
            profile = Profile(
                name=f"DEF024 Profile {suffix}",
                is_active=False,
                is_default=False,
                is_editable=True,
                version=0,
            )
            db.add(profile)
            await db.flush()
            profile_id = profile.id
            vendor = (
                await db.execute(
                    select(Vendor)
                    .where(Vendor.key == "openai")
                    .order_by(Vendor.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if vendor is None:
                db.add(
                    Vendor(
                        key="openai",
                        name=f"DEF024 OpenAI {suffix}",
                    )
                )
                await db.flush()
            response = await import_config(data=payload, db=db, profile_id=profile_id)
            await db.commit()
            assert response.endpoints_imported == 1
            assert response.connections_imported == 1

        async with AsyncSessionLocal() as db:
            endpoint = (
                await db.execute(
                    select(Endpoint).where(
                        Endpoint.profile_id == profile_id,
                        Endpoint.name == endpoint_name,
                    )
                )
            ).scalar_one()
            connection = (
                await db.execute(
                    select(Connection).where(
                        Connection.profile_id == profile_id,
                        Connection.name == connection_name,
                    )
                )
            ).scalar_one()
            fx_row = (
                await db.execute(
                    select(EndpointFxRateSetting).where(
                        EndpointFxRateSetting.profile_id == profile_id,
                        EndpointFxRateSetting.model_id == model_id,
                        EndpointFxRateSetting.endpoint_id == endpoint.id,
                    )
                )
            ).scalar_one_or_none()

            assert isinstance(endpoint.id, int) and endpoint.id > 0
            assert isinstance(connection.id, int) and connection.id > 0
            assert connection.endpoint_id == endpoint.id
            assert connection.qps_limit == 3
            assert connection.max_in_flight_non_stream == 5
            assert connection.max_in_flight_stream == 2
            assert fx_row is not None

            export_response = await export_config(db=db, profile_id=profile_id)
            exported = json.loads(bytes(export_response.body).decode("utf-8"))

        exported_endpoint = next(
            e for e in exported["endpoints"] if e["name"] == endpoint_name
        )
        assert "endpoint_id" not in exported_endpoint

        exported_model = next(
            m for m in exported["models"] if m["model_id"] == model_id
        )
        exported_connection = next(
            c for c in exported_model["connections"] if c["name"] == connection_name
        )
        assert "connection_id" not in exported_connection
        assert "endpoint_id" not in exported_connection
        assert "pricing_template_id" not in exported_connection
        assert exported_connection["endpoint_name"] == endpoint_name
        assert exported_connection["qps_limit"] == 3
        assert exported_connection["max_in_flight_non_stream"] == 5
        assert exported_connection["max_in_flight_stream"] == 2

        exported_mapping = next(
            m
            for m in exported["user_settings"]["endpoint_fx_mappings"]
            if m["model_id"] == model_id
        )
        assert "endpoint_id" not in exported_mapping
        assert exported_mapping["endpoint_name"] == endpoint_name

        from app.routers.endpoints import create_endpoint
        from app.schemas.schemas import EndpointCreate

        async with AsyncSessionLocal() as db:
            created_endpoint = await create_endpoint(
                body=EndpointCreate(
                    name=f"{endpoint_name}-follow-up",
                    base_url="https://post-import.example.com",
                    api_key="sk-post-import",
                ),
                db=db,
                profile_id=profile_id,
            )
            await db.commit()

        assert created_endpoint.id > endpoint.id

    @pytest.mark.asyncio
    async def test_import_allocates_unique_connection_ids_with_name_based_payload(self):
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import Connection, Profile, Vendor
        from app.routers.config import export_config, import_config
        from app.schemas.schemas import ConfigImportRequest

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        endpoint_a_name = f"def024-duplicate-id-endpoint-a-{suffix}"
        endpoint_b_name = f"def024-duplicate-id-endpoint-b-{suffix}"
        model_id = f"def024-duplicate-id-model-{suffix}"
        connection_a_name = f"def024-duplicate-id-connection-a-{suffix}"
        connection_b_name = f"def024-duplicate-id-connection-b-{suffix}"
        payload = ConfigImportRequest.model_validate(
            {
                "version": 6,
                "vendors": [
                    {
                        "key": "openai",
                        "name": "OpenAI",
                        "description": None,
                        "audit_enabled": False,
                        "audit_capture_bodies": True,
                    }
                ],
                "endpoints": [
                    {
                        "name": endpoint_a_name,
                        "base_url": "https://api.openai.com",
                        "api_key": "sk-test-a",
                    },
                    {
                        "name": endpoint_b_name,
                        "base_url": "https://api.openai.com",
                        "api_key": "sk-test-b",
                    },
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
                        "vendor_key": "openai",
                        "api_family": "openai",
                        "model_id": model_id,
                        "model_type": "native",
                        "loadbalance_strategy_name": "single-primary",
                        "connections": [
                            {
                                "endpoint_name": endpoint_a_name,
                                "name": connection_a_name,
                                "qps_limit": 7,
                                "max_in_flight_non_stream": 3,
                                "max_in_flight_stream": 1,
                            },
                            {
                                "endpoint_name": endpoint_b_name,
                                "name": connection_b_name,
                                "qps_limit": 11,
                                "max_in_flight_non_stream": 4,
                                "max_in_flight_stream": 2,
                            },
                        ],
                    }
                ],
            }
        )

        async with AsyncSessionLocal() as db:
            profile = Profile(
                name=f"DEF024 Duplicate IDs Profile {suffix}",
                is_active=False,
                is_default=False,
                is_editable=True,
                version=0,
            )
            db.add(profile)
            await db.flush()
            profile_id = profile.id
            vendor = (
                await db.execute(
                    select(Vendor)
                    .where(Vendor.key == "openai")
                    .order_by(Vendor.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if vendor is None:
                db.add(
                    Vendor(
                        key="openai",
                        name=f"DEF024 Duplicate ID OpenAI {suffix}",
                    )
                )
                await db.flush()

            response = await import_config(data=payload, db=db, profile_id=profile_id)
            await db.commit()
            assert response.endpoints_imported == 2
            assert response.connections_imported == 2

        async with AsyncSessionLocal() as db:
            connections = (
                (
                    await db.execute(
                        select(Connection)
                        .where(
                            Connection.profile_id == profile_id,
                            Connection.name.in_([connection_a_name, connection_b_name]),
                        )
                        .order_by(Connection.name.asc())
                    )
                )
                .scalars()
                .all()
            )

            assert len(connections) == 2
            assert len({connection.id for connection in connections}) == 2

            export_response = await export_config(db=db, profile_id=profile_id)
            exported = json.loads(bytes(export_response.body).decode("utf-8"))

        exported_model = next(
            model for model in exported["models"] if model["model_id"] == model_id
        )
        exported_connections = sorted(
            exported_model["connections"],
            key=lambda connection: connection["name"],
        )
        assert [connection["endpoint_name"] for connection in exported_connections] == [
            endpoint_a_name,
            endpoint_b_name,
        ]
        assert all(
            "connection_id" not in connection for connection in exported_connections
        )
        assert all(
            "endpoint_id" not in connection for connection in exported_connections
        )
        assert all(
            "pricing_template_id" not in connection
            for connection in exported_connections
        )
        assert [connection["qps_limit"] for connection in exported_connections] == [
            7,
            11,
        ]
        assert [
            connection["max_in_flight_non_stream"]
            for connection in exported_connections
        ] == [
            3,
            4,
        ]
        assert [
            connection["max_in_flight_stream"] for connection in exported_connections
        ] == [
            1,
            2,
        ]


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

            profile = Profile(
                name="DEF026 Profile",
                is_active=False,
                is_default=False,
                is_editable=True,
                version=0,
            )
            db.add(profile)
            await db.flush()
            payload = ConfigImportRequest.model_validate(
                {
                    "version": 6,
                    "endpoints": [],
                    "vendors": [],
                    "models": [],
                    "pricing_templates": [],
                    "loadbalance_strategies": [],
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

            response = await import_config(data=payload, db=db, profile_id=profile.id)

            assert response.endpoints_imported == 0
            assert response.models_imported == 0
            assert response.connections_imported == 0
            assert response.connections_imported == 0

            await db.rollback()


class TestDEF082_ProxyTargetConfigRoundtrip:
    @pytest.mark.asyncio
    async def test_proxy_target_config_roundtrip_preserves_order(self):
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import ModelConfig, Profile, Vendor
        from app.routers.config import export_config, import_config
        from app.schemas.schemas import ConfigImportRequest

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        target_a_model_id = f"def082-proxy-target-a-{suffix}"
        target_b_model_id = f"def082-proxy-target-b-{suffix}"
        proxy_model_id = f"def082-proxy-model-{suffix}"

        payload = ConfigImportRequest.model_validate(
            {
                "version": 6,
                "vendors": [
                    {
                        "key": "openai",
                        "name": "OpenAI",
                        "description": None,
                        "audit_enabled": False,
                        "audit_capture_bodies": True,
                    }
                ],
                "endpoints": [],
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
                        "vendor_key": "openai",
                        "api_family": "openai",
                        "model_id": target_a_model_id,
                        "model_type": "native",
                        "loadbalance_strategy_name": "single-primary",
                        "connections": [],
                    },
                    {
                        "vendor_key": "openai",
                        "api_family": "openai",
                        "model_id": target_b_model_id,
                        "model_type": "native",
                        "loadbalance_strategy_name": "single-primary",
                        "connections": [],
                    },
                    {
                        "vendor_key": "openai",
                        "api_family": "openai",
                        "model_id": proxy_model_id,
                        "model_type": "proxy",
                        "proxy_targets": [
                            {"target_model_id": target_a_model_id, "position": 0},
                            {"target_model_id": target_b_model_id, "position": 1},
                        ],
                        "connections": [],
                    },
                ],
            }
        )

        async with AsyncSessionLocal() as db:
            profile = Profile(
                name=f"DEF082 Proxy Targets Profile {suffix}",
                is_active=False,
                is_default=False,
                is_editable=True,
                version=0,
            )
            db.add(profile)
            await db.flush()
            profile_id = profile.id

            vendor = (
                await db.execute(
                    select(Vendor)
                    .where(Vendor.key == "openai")
                    .order_by(Vendor.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if vendor is None:
                db.add(
                    Vendor(
                        key="openai",
                        name=f"DEF082 Proxy Target OpenAI {suffix}",
                    )
                )
                await db.flush()

            response = await import_config(data=payload, db=db, profile_id=profile_id)
            await db.commit()
            assert response.models_imported == 3

        async with AsyncSessionLocal() as db:
            proxy_model = (
                await db.execute(
                    select(ModelConfig).where(
                        ModelConfig.profile_id == profile_id,
                        ModelConfig.model_id == proxy_model_id,
                    )
                )
            ).scalar_one()

            export_response = await export_config(db=db, profile_id=profile_id)
            exported = json.loads(bytes(export_response.body).decode("utf-8"))

        assert proxy_model.model_type == "proxy"
        exported_proxy_model = next(
            model for model in exported["models"] if model["model_id"] == proxy_model_id
        )
        assert exported_proxy_model["proxy_targets"] == [
            {"target_model_id": target_a_model_id, "position": 0},
            {"target_model_id": target_b_model_id, "position": 1},
        ]
        assert "redirect_to" not in exported_proxy_model
