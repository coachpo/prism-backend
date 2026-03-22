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
    async def test_import_export_roundtrip_omits_id_fields(self):
        from sqlalchemy import select
        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import (
            Connection,
            Endpoint,
            EndpointFxRateSetting,
            Profile,
            Provider,
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
    async def test_import_allocates_unique_connection_ids_when_payload_ids_repeat(self):
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import Connection, Profile, Provider
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
                "version": 2,
                "endpoints": [
                    {
                        "endpoint_id": 9001,
                        "name": endpoint_a_name,
                        "base_url": "https://api.openai.com/v1",
                        "api_key": "sk-test-a",
                    },
                    {
                        "endpoint_id": 9002,
                        "name": endpoint_b_name,
                        "base_url": "https://api.openai.com/v1",
                        "api_key": "sk-test-b",
                    },
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
                                "name": connection_a_name,
                            },
                            {
                                "connection_id": 7001,
                                "endpoint_id": 9002,
                                "name": connection_b_name,
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
                        name=f"DEF024 Duplicate ID OpenAI {suffix}",
                        provider_type="openai",
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

            response = await import_config(data=payload, db=db, profile_id=profile.id)

            assert response.endpoints_imported == 0
            assert response.models_imported == 0
            assert response.connections_imported == 0
            assert response.connections_imported == 0

            await db.rollback()
