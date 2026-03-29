import asyncio
import json
from uuid import uuid4

import pytest

from tests.loadbalance_strategy_helpers import (
    make_auto_recovery_disabled,
    make_auto_recovery_enabled,
    make_loadbalance_strategy,
)
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, text


def _unique_suffix() -> str:
    return f"{int(asyncio.get_running_loop().time() * 1_000_000)}-{uuid4().hex[:8]}"


def _vendor_key_for_api_family(api_family: str) -> str:
    return "google" if api_family == "gemini" else api_family


async def _get_or_create_vendor(db, *, api_family: str = "openai"):
    from app.models.models import Vendor

    vendor_key = _vendor_key_for_api_family(api_family)
    vendor = (
        (
            await db.execute(
                select(Vendor)
                .where(Vendor.key == vendor_key)
                .order_by(Vendor.id.asc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if vendor is not None:
        return vendor

    vendor = Vendor(key=vendor_key, name=f"{vendor_key.title()} {_unique_suffix()}")
    db.add(vendor)
    await db.flush()
    return vendor


async def _get_round_robin_cursor_row(db, *, profile_id: int, model_config_id: int):
    return (
        (
            await db.execute(
                text(
                    "SELECT next_cursor FROM loadbalance_round_robin_state "
                    "WHERE profile_id = :profile_id AND model_config_id = :model_config_id"
                ),
                {"profile_id": profile_id, "model_config_id": model_config_id},
            )
        )
        .mappings()
        .first()
    )


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
        vendor = await _get_or_create_vendor(db)
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
            vendor_id=vendor.id,
            api_family="openai",
            model_id=f"def067-model-{suffix}",
            model_type="native",
            loadbalance_strategy=make_loadbalance_strategy(
                profile_id=profile.id,
                strategy_type="failover",
            ),
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


@pytest.mark.asyncio
async def test_connection_mutations_clear_round_robin_cursor_state():
    from app.core.database import AsyncSessionLocal, get_engine
    from app.models.models import Endpoint, ModelConfig, Profile
    from app.routers.connections import (
        create_connection,
        delete_connection,
        move_connection_priority,
        update_connection,
    )
    from app.schemas.schemas import (
        ConnectionCreate,
        ConnectionPriorityMoveRequest,
        ConnectionUpdate,
    )
    from app.services.loadbalancer.planner import (
        build_attempt_plan,
        get_model_config_with_connections,
    )

    await get_engine().dispose()

    suffix = _unique_suffix()

    async with AsyncSessionLocal() as db:
        vendor = await _get_or_create_vendor(db)
        profile = Profile(
            name=f"RR Cursor Reset Profile {suffix}",
            is_active=False,
            is_default=False,
            is_editable=True,
            version=0,
        )
        db.add(profile)
        await db.flush()

        model = ModelConfig(
            profile_id=profile.id,
            vendor_id=vendor.id,
            api_family="openai",
            model_id=f"rr-reset-model-{suffix}",
            model_type="native",
            loadbalance_strategy=make_loadbalance_strategy(
                profile_id=profile.id,
                strategy_type="round-robin",
            ),
            is_enabled=True,
        )
        db.add(model)
        await db.flush()

        endpoints: list[Endpoint] = []
        for index, label in enumerate(("Alpha", "Bravo", "Charlie")):
            endpoint = Endpoint(
                profile_id=profile.id,
                name=f"RR Cursor {label} {suffix}",
                base_url=f"https://rr-cursor-{label.lower()}.{suffix}.example.com",
                api_key=f"sk-rr-{label.lower()}",
                position=index,
            )
            db.add(endpoint)
            endpoints.append(endpoint)
        await db.commit()

        first = await create_connection(
            model_config_id=model.id,
            body=ConnectionCreate(endpoint_id=endpoints[0].id, name=f"first-{suffix}"),
            db=db,
            profile_id=profile.id,
        )
        second = await create_connection(
            model_config_id=model.id,
            body=ConnectionCreate(endpoint_id=endpoints[1].id, name=f"second-{suffix}"),
            db=db,
            profile_id=profile.id,
        )
        await db.commit()

        async with AsyncSessionLocal() as verify_db:
            resolved = await get_model_config_with_connections(
                verify_db, profile.id, model.model_id
            )
            assert resolved is not None
            await build_attempt_plan(verify_db, profile.id, resolved, None)
            assert (
                await _get_round_robin_cursor_row(
                    verify_db, profile_id=profile.id, model_config_id=model.id
                )
            ) == {"next_cursor": 1}

        third = await create_connection(
            model_config_id=model.id,
            body=ConnectionCreate(endpoint_id=endpoints[2].id, name=f"third-{suffix}"),
            db=db,
            profile_id=profile.id,
        )
        assert (
            await _get_round_robin_cursor_row(
                db, profile_id=profile.id, model_config_id=model.id
            )
        ) is None
        await db.commit()

        async with AsyncSessionLocal() as verify_db:
            resolved_after_create = await get_model_config_with_connections(
                verify_db, profile.id, model.model_id
            )
            assert resolved_after_create is not None
            await build_attempt_plan(verify_db, profile.id, resolved_after_create, None)
            assert (
                await _get_round_robin_cursor_row(
                    verify_db, profile_id=profile.id, model_config_id=model.id
                )
            ) == {"next_cursor": 1}

        await move_connection_priority(
            model_config_id=model.id,
            connection_id=third.id,
            body=ConnectionPriorityMoveRequest(to_index=0),
            db=db,
            profile_id=profile.id,
        )
        assert (
            await _get_round_robin_cursor_row(
                db, profile_id=profile.id, model_config_id=model.id
            )
        ) is None
        await db.commit()

        async with AsyncSessionLocal() as verify_db:
            resolved_after_reorder = await get_model_config_with_connections(
                verify_db, profile.id, model.model_id
            )
            assert resolved_after_reorder is not None
            await build_attempt_plan(
                verify_db, profile.id, resolved_after_reorder, None
            )
            assert (
                await _get_round_robin_cursor_row(
                    verify_db, profile_id=profile.id, model_config_id=model.id
                )
            ) == {"next_cursor": 1}

        await update_connection(
            connection_id=first.id,
            body=ConnectionUpdate(is_active=False),
            db=db,
            profile_id=profile.id,
        )
        assert (
            await _get_round_robin_cursor_row(
                db, profile_id=profile.id, model_config_id=model.id
            )
        ) is None
        await db.commit()

        async with AsyncSessionLocal() as verify_db:
            resolved_after_disable = await get_model_config_with_connections(
                verify_db, profile.id, model.model_id
            )
            assert resolved_after_disable is not None
            await build_attempt_plan(
                verify_db, profile.id, resolved_after_disable, None
            )
            assert (
                await _get_round_robin_cursor_row(
                    verify_db, profile_id=profile.id, model_config_id=model.id
                )
            ) == {"next_cursor": 1}

        await delete_connection(connection_id=second.id, db=db, profile_id=profile.id)
        assert (
            await _get_round_robin_cursor_row(
                db, profile_id=profile.id, model_config_id=model.id
            )
        ) is None
        await db.commit()


def test_connection_priority_validation_and_loadbalancer_tie_break():
    from app.models.models import Connection, Endpoint, ModelConfig
    from app.schemas.schemas import (
        ConfigConnectionExport,
        ConnectionCreate,
        ConnectionUpdate,
    )
    from app.services.loadbalancer.planner import get_active_connections

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
        vendor_id=1,
        api_family="openai",
        model_id="def067-model",
        model_type="native",
        loadbalance_strategy=make_loadbalance_strategy(
            profile_id=1,
            strategy_type="failover",
        ),
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
        vendor = await _get_or_create_vendor(db)

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
                "version": 1,
                "vendors": [
                    {
                        "key": vendor.key,
                        "name": vendor.name,
                        "description": vendor.description,
                        "icon_key": vendor.icon_key,
                        "audit_enabled": vendor.audit_enabled,
                        "audit_capture_bodies": vendor.audit_capture_bodies,
                    }
                ],
                "endpoints": [
                    {
                        "name": f"DEF068 E0 {suffix}",
                        "base_url": f"https://def068-e0.{suffix}.example.com",
                        "api_key": "sk-def068-e0",
                    },
                    {
                        "name": f"DEF068 E1 {suffix}",
                        "base_url": f"https://def068-e1.{suffix}.example.com",
                        "api_key": "sk-def068-e1",
                    },
                    {
                        "name": f"DEF068 E2 {suffix}",
                        "base_url": f"https://def068-e2.{suffix}.example.com",
                        "api_key": "sk-def068-e2",
                    },
                ],
                "pricing_templates": [],
                "loadbalance_strategies": [
                    {
                        "name": "failover-primary",
                        "strategy_type": "failover",
                        "auto_recovery": make_auto_recovery_enabled(
                            status_codes=[429, 503]
                        ),
                    }
                ],
                "models": [
                    {
                        "vendor_key": "openai",
                        "api_family": "openai",
                        "model_id": f"def068-model-{suffix}",
                        "model_type": "native",
                        "loadbalance_strategy_name": "failover-primary",
                        "connections": [
                            {
                                "endpoint_name": f"DEF068 E1 {suffix}",
                                "priority": 5,
                                "name": "Second in payload",
                                "is_active": True,
                            },
                            {
                                "endpoint_name": f"DEF068 E0 {suffix}",
                                "priority": 5,
                                "name": "First in payload",
                                "is_active": True,
                            },
                            {
                                "endpoint_name": f"DEF068 E2 {suffix}",
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
async def test_connection_priority_migration_normalizes_existing_rows():
    from app.core.database import AsyncSessionLocal, get_engine
    from app.models.models import Connection, Endpoint, ModelConfig, Profile
    from app.routers.connections import move_connection_priority
    from app.schemas.schemas import ConnectionPriorityMoveRequest

    await get_engine().dispose()

    suffix = _unique_suffix()

    async with AsyncSessionLocal() as db:
        vendor = await _get_or_create_vendor(db)
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
            vendor_id=vendor.id,
            api_family="openai",
            model_id=f"def069-model-{suffix}",
            model_type="native",
            loadbalance_strategy=make_loadbalance_strategy(
                profile_id=profile.id,
                strategy_type="failover",
            ),
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

        assert [connection.priority for connection in normalized] == [7, 7, 99]

        reordered = await move_connection_priority(
            model_config_id=model.id,
            connection_id=normalized[1].id,
            body=ConnectionPriorityMoveRequest(to_index=0),
            db=db,
            profile_id=profile.id,
        )

        assert [connection.priority for connection in reordered] == [0, 1, 2]
