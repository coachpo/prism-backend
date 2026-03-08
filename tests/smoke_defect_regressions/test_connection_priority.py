import asyncio
import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select


def _unique_suffix() -> str:
    return f"{int(asyncio.get_running_loop().time() * 1_000_000)}-{uuid4().hex[:8]}"


async def _get_or_create_provider(db, *, provider_type: str = "openai"):
    from app.models.models import Provider

    provider = (
        (
            await db.execute(
                select(Provider)
                .where(Provider.provider_type == provider_type)
                .order_by(Provider.id.asc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if provider is not None:
        return provider

    provider = Provider(
        name=f"{provider_type.title()} {_unique_suffix()}", provider_type=provider_type
    )
    db.add(provider)
    await db.flush()
    return provider


@pytest.mark.asyncio
async def test_connection_priority_crud_flow():
    from app.core.database import AsyncSessionLocal, get_engine
    from app.models.models import Endpoint, ModelConfig, Profile
    from app.routers.connections import (
        create_connection,
        delete_connection,
        list_connections,
        move_connection_priority,
    )
    from app.schemas.schemas import ConnectionCreate, ConnectionPriorityMoveRequest

    await get_engine().dispose()

    suffix = _unique_suffix()

    async with AsyncSessionLocal() as db:
        provider = await _get_or_create_provider(db)
        profile = Profile(
            name=f"DEF067 Profile {suffix}",
            is_active=False,
            is_default=False,
            is_editable=True,
            version=0,
        )
        db.add(profile)
        await db.flush()

        model = ModelConfig(
            profile_id=profile.id,
            provider_id=provider.id,
            model_id=f"def067-model-{suffix}",
            model_type="native",
            lb_strategy="failover",
            failover_recovery_enabled=True,
            failover_recovery_cooldown_seconds=60,
            is_enabled=True,
        )
        db.add(model)
        await db.flush()

        endpoints: list[Endpoint] = []
        for index, label in enumerate(("Alpha", "Bravo", "Charlie")):
            endpoint = Endpoint(
                profile_id=profile.id,
                name=f"DEF067 {label} {suffix}",
                base_url=f"https://def067-{label.lower()}.{suffix}.example.com",
                api_key=f"sk-def067-{label.lower()}",
                position=index,
            )
            db.add(endpoint)
            endpoints.append(endpoint)
        await db.flush()

        created = []
        for index, endpoint in enumerate(endpoints):
            connection = await create_connection(
                model_config_id=model.id,
                body=ConnectionCreate(
                    endpoint_id=endpoint.id,
                    name=f"DEF067 connection {index} {suffix}",
                ),
                db=db,
                profile_id=profile.id,
            )
            created.append(connection)
            assert connection.priority == index

        listed = await list_connections(
            db=db, model_config_id=model.id, profile_id=profile.id
        )
        assert [connection.id for connection in listed] == [
            connection.id for connection in created
        ]
        assert [connection.priority for connection in listed] == [0, 1, 2]

        moved_up = await move_connection_priority(
            model_config_id=model.id,
            connection_id=created[2].id,
            body=ConnectionPriorityMoveRequest(to_index=0),
            db=db,
            profile_id=profile.id,
        )
        assert [connection.id for connection in moved_up] == [
            created[2].id,
            created[0].id,
            created[1].id,
        ]
        assert [connection.priority for connection in moved_up] == [0, 1, 2]

        moved_down = await move_connection_priority(
            model_config_id=model.id,
            connection_id=created[2].id,
            body=ConnectionPriorityMoveRequest(to_index=2),
            db=db,
            profile_id=profile.id,
        )
        assert [connection.id for connection in moved_down] == [
            created[0].id,
            created[1].id,
            created[2].id,
        ]
        assert [connection.priority for connection in moved_down] == [0, 1, 2]

        stable = await move_connection_priority(
            model_config_id=model.id,
            connection_id=created[1].id,
            body=ConnectionPriorityMoveRequest(to_index=1),
            db=db,
            profile_id=profile.id,
        )
        assert [connection.id for connection in stable] == [
            created[0].id,
            created[1].id,
            created[2].id,
        ]

        with pytest.raises(HTTPException) as exc_info:
            await move_connection_priority(
                model_config_id=model.id,
                connection_id=created[2].id,
                body=ConnectionPriorityMoveRequest(to_index=5),
                db=db,
                profile_id=profile.id,
            )
        assert exc_info.value.status_code == 422
        assert "to_index must be between 0 and 2" in str(exc_info.value.detail)

        response = await delete_connection(
            connection_id=created[1].id,
            db=db,
            profile_id=profile.id,
        )
        assert response == {"deleted": True}

        remaining = await list_connections(
            db=db, model_config_id=model.id, profile_id=profile.id
        )
        assert [connection.id for connection in remaining] == [
            created[0].id,
            created[2].id,
        ]
        assert [connection.priority for connection in remaining] == [0, 1]


def test_connection_priority_validation_and_loadbalancer_tie_break():
    from app.models.models import Connection, Endpoint, ModelConfig
    from app.schemas.schemas import (
        ConfigConnectionExport,
        ConnectionCreate,
        ConnectionUpdate,
    )
    from app.services.loadbalancer import get_active_connections

    with pytest.raises(ValidationError):
        ConnectionCreate.model_validate(
            {
                "endpoint_id": 1,
                "priority": 0,
            }
        )

    with pytest.raises(ValidationError):
        ConnectionUpdate.model_validate({"priority": 1})

    with pytest.raises(ValidationError):
        ConfigConnectionExport.model_validate(
            {
                "connection_id": 1,
                "endpoint_id": 1,
                "priority": -1,
            }
        )

    endpoint = Endpoint(
        id=1,
        profile_id=1,
        name="def067-endpoint",
        base_url="https://example.com",
        api_key="sk-test",
        position=0,
    )
    lower_id = Connection(
        id=7,
        profile_id=1,
        model_config_id=1,
        endpoint_id=1,
        priority=0,
        is_active=True,
    )
    higher_id = Connection(
        id=9,
        profile_id=1,
        model_config_id=1,
        endpoint_id=1,
        priority=0,
        is_active=True,
    )
    later_priority = Connection(
        id=11,
        profile_id=1,
        model_config_id=1,
        endpoint_id=1,
        priority=1,
        is_active=True,
    )

    for connection in (lower_id, higher_id, later_priority):
        connection.endpoint_rel = endpoint

    model_config = ModelConfig(
        id=1,
        profile_id=1,
        provider_id=1,
        model_id="def067-model",
        model_type="native",
        lb_strategy="failover",
        failover_recovery_enabled=True,
        failover_recovery_cooldown_seconds=60,
        is_enabled=True,
        connections=[later_priority, higher_id, lower_id],
    )

    ordered = get_active_connections(model_config)
    assert [connection.id for connection in ordered] == [7, 9, 11]


@pytest.mark.asyncio
async def test_connection_priority_import_normalizes_and_preserves_payload_order():
    from app.core.database import AsyncSessionLocal, get_engine
    from app.models.models import Profile
    from app.routers.config import export_config, import_config
    from app.schemas.schemas import ConfigImportRequest

    await get_engine().dispose()

    suffix = _unique_suffix()

    async with AsyncSessionLocal() as db:
        await _get_or_create_provider(db)

        profile = Profile(
            name=f"DEF068 Profile {suffix}",
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
                "endpoints": [
                    {
                        "endpoint_id": 2000,
                        "name": f"DEF068 E0 {suffix}",
                        "base_url": f"https://def068-e0.{suffix}.example.com",
                        "api_key": "sk-def068-e0",
                    },
                    {
                        "endpoint_id": 2001,
                        "name": f"DEF068 E1 {suffix}",
                        "base_url": f"https://def068-e1.{suffix}.example.com",
                        "api_key": "sk-def068-e1",
                    },
                    {
                        "endpoint_id": 2002,
                        "name": f"DEF068 E2 {suffix}",
                        "base_url": f"https://def068-e2.{suffix}.example.com",
                        "api_key": "sk-def068-e2",
                    },
                ],
                "pricing_templates": [],
                "models": [
                    {
                        "provider_type": "openai",
                        "model_id": f"def068-model-{suffix}",
                        "model_type": "native",
                        "connections": [
                            {
                                "connection_id": 9002,
                                "endpoint_id": 2001,
                                "priority": 5,
                                "name": "Second in payload",
                                "is_active": True,
                            },
                            {
                                "connection_id": 9001,
                                "endpoint_id": 2000,
                                "priority": 5,
                                "name": "First in payload",
                                "is_active": True,
                            },
                            {
                                "connection_id": 9003,
                                "endpoint_id": 2002,
                                "priority": 9,
                                "name": "Third in payload",
                                "is_active": True,
                            },
                        ],
                    }
                ],
            }
        )

        response = await import_config(data=payload, db=db, profile_id=profile.id)
        assert response.connections_imported == 3

        exported = await export_config(db=db, profile_id=profile.id)
        body = json.loads(bytes(exported.body).decode("utf-8"))
        connections = body["models"][0]["connections"]

        assert [connection["name"] for connection in connections] == [
            "Second in payload",
            "First in payload",
            "Third in payload",
        ]
        assert [connection["priority"] for connection in connections] == [0, 1, 2]


@pytest.mark.asyncio
async def test_connection_priority_migration_normalizes_existing_rows(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.core.database import AsyncSessionLocal, get_engine
    from app.models.models import Connection, Endpoint, ModelConfig, Profile

    await get_engine().dispose()

    suffix = _unique_suffix()
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0005_connection_priority_normalization.py"
    )
    spec = importlib.util.spec_from_file_location(
        "prism_conn_priority_migration", migration_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load connection priority migration module")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    async with AsyncSessionLocal() as db:
        provider = await _get_or_create_provider(db)
        profile = Profile(
            name=f"DEF069 Profile {suffix}",
            is_active=False,
            is_default=False,
            is_editable=True,
            version=0,
        )
        db.add(profile)
        await db.flush()

        model = ModelConfig(
            profile_id=profile.id,
            provider_id=provider.id,
            model_id=f"def069-model-{suffix}",
            model_type="native",
            lb_strategy="failover",
            failover_recovery_enabled=True,
            failover_recovery_cooldown_seconds=60,
            is_enabled=True,
        )
        db.add(model)
        await db.flush()

        endpoint_ids: list[int] = []
        for index, label in enumerate(("Alpha", "Bravo", "Charlie")):
            endpoint = Endpoint(
                profile_id=profile.id,
                name=f"DEF069 {label} {suffix}",
                base_url=f"https://def069-{label.lower()}.{suffix}.example.com",
                api_key=f"sk-def069-{label.lower()}",
                position=index,
            )
            db.add(endpoint)
            await db.flush()
            endpoint_ids.append(endpoint.id)

        for endpoint_id, priority in zip(endpoint_ids, (7, 7, 99), strict=True):
            db.add(
                Connection(
                    profile_id=profile.id,
                    model_config_id=model.id,
                    endpoint_id=endpoint_id,
                    priority=priority,
                    is_active=True,
                )
            )
        await db.flush()

        async def run_upgrade() -> None:
            def apply_upgrade(sync_session):
                monkeypatch.setattr(migration.op, "execute", sync_session.execute)
                migration.upgrade()

            await db.run_sync(apply_upgrade)

        await run_upgrade()

        normalized = (
            (
                await db.execute(
                    select(Connection)
                    .where(Connection.model_config_id == model.id)
                    .order_by(Connection.id.asc())
                )
            )
            .scalars()
            .all()
        )

        assert [connection.priority for connection in normalized] == [0, 1, 2]
