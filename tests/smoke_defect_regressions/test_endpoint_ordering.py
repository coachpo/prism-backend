import asyncio
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException


def _unique_suffix() -> str:
    return f"{int(asyncio.get_running_loop().time() * 1_000_000)}-{uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_endpoint_position_crud_flow():
    from app.core.database import AsyncSessionLocal, get_engine
    from app.core.crypto import decrypt_secret
    from app.models.models import Profile
    from app.routers.endpoints import (
        create_endpoint,
        delete_endpoint,
        duplicate_endpoint,
        list_endpoints,
        move_endpoint_position,
    )
    from app.schemas.schemas import EndpointCreate, EndpointPositionMoveRequest

    await get_engine().dispose()

    suffix = _unique_suffix()

    async with AsyncSessionLocal() as db:
        profile = Profile(
            name=f"DEF062 Profile {suffix}",
            is_active=False,
            is_default=False,
            is_editable=True,
            version=0,
        )
        db.add(profile)
        await db.flush()

        created = []
        for index, label in enumerate(("Alpha", "Bravo", "Charlie")):
            endpoint = await create_endpoint(
                body=EndpointCreate(
                    name=f"DEF062 {label} {suffix}",
                    base_url=f"https://def062-{index}.{suffix}.example.com",
                    api_key=f"sk-def062-{label.lower()}",
                ),
                db=db,
                profile_id=profile.id,
            )
            created.append(endpoint)

        assert [endpoint.position for endpoint in created] == [0, 1, 2]

        listed = await list_endpoints(db=db, profile_id=profile.id)
        assert [endpoint.id for endpoint in listed] == [
            endpoint.id for endpoint in created
        ]
        assert [endpoint.position for endpoint in listed] == [0, 1, 2]

        moved = await move_endpoint_position(
            endpoint_id=created[2].id,
            body=EndpointPositionMoveRequest(to_index=0),
            db=db,
            profile_id=profile.id,
        )
        assert [endpoint.id for endpoint in moved] == [
            created[2].id,
            created[0].id,
            created[1].id,
        ]
        assert [endpoint.position for endpoint in moved] == [0, 1, 2]

        stable = await move_endpoint_position(
            endpoint_id=created[2].id,
            body=EndpointPositionMoveRequest(to_index=0),
            db=db,
            profile_id=profile.id,
        )
        assert [endpoint.id for endpoint in stable] == [
            created[2].id,
            created[0].id,
            created[1].id,
        ]

        with pytest.raises(HTTPException) as exc_info:
            await move_endpoint_position(
                endpoint_id=created[2].id,
                body=EndpointPositionMoveRequest(to_index=5),
                db=db,
                profile_id=profile.id,
            )
        assert exc_info.value.status_code == 422
        assert "to_index must be between 0 and 2" in str(exc_info.value.detail)

        response = await delete_endpoint(
            endpoint_id=created[0].id,
            db=db,
            profile_id=profile.id,
        )
        assert response == {"deleted": True}

        remaining = await list_endpoints(db=db, profile_id=profile.id)
        assert [endpoint.id for endpoint in remaining] == [created[2].id, created[1].id]
        assert [endpoint.position for endpoint in remaining] == [0, 1]

        duplicate_source = created[2]
        first_duplicate = await duplicate_endpoint(
            endpoint_id=duplicate_source.id,
            db=db,
            profile_id=profile.id,
        )
        second_duplicate = await duplicate_endpoint(
            endpoint_id=duplicate_source.id,
            db=db,
            profile_id=profile.id,
        )

        assert first_duplicate.id != duplicate_source.id
        assert first_duplicate.name == f"{duplicate_source.name} copy"
        assert second_duplicate.name == f"{duplicate_source.name} copy 2"
        assert first_duplicate.base_url == duplicate_source.base_url
        assert decrypt_secret(first_duplicate.api_key) == decrypt_secret(
            duplicate_source.api_key
        )

        duplicated_listing = await list_endpoints(db=db, profile_id=profile.id)
        assert [endpoint.name for endpoint in duplicated_listing] == [
            created[2].name,
            created[1].name,
            f"{duplicate_source.name} copy",
            f"{duplicate_source.name} copy 2",
        ]
        assert [endpoint.position for endpoint in duplicated_listing] == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_endpoint_position_export_import_and_profile_isolation():
    from app.core.database import AsyncSessionLocal, get_engine
    from app.models.models import Profile
    from app.routers.config import export_config, import_config
    from app.routers.endpoints import (
        create_endpoint,
        list_endpoints,
        move_endpoint_position,
    )
    from app.schemas.schemas import (
        ConfigImportRequest,
        EndpointCreate,
        EndpointPositionMoveRequest,
    )

    await get_engine().dispose()

    suffix = _unique_suffix()

    async with AsyncSessionLocal() as db:
        profile_a = Profile(
            name=f"DEF063 Profile A {suffix}",
            is_active=False,
            is_default=False,
            is_editable=True,
            version=0,
        )
        profile_b = Profile(
            name=f"DEF063 Profile B {suffix}",
            is_active=False,
            is_default=False,
            is_editable=True,
            version=0,
        )
        db.add_all([profile_a, profile_b])
        await db.flush()

        await create_endpoint(
            body=EndpointCreate(
                name=f"DEF063 A First {suffix}",
                base_url=f"https://def063-a-first.{suffix}.example.com",
                api_key="sk-a-first",
            ),
            db=db,
            profile_id=profile_a.id,
        )
        a_second = await create_endpoint(
            body=EndpointCreate(
                name=f"DEF063 A Second {suffix}",
                base_url=f"https://def063-a-second.{suffix}.example.com",
                api_key="sk-a-second",
            ),
            db=db,
            profile_id=profile_a.id,
        )
        b_only = await create_endpoint(
            body=EndpointCreate(
                name=f"DEF063 B Only {suffix}",
                base_url=f"https://def063-b-only.{suffix}.example.com",
                api_key="sk-b-only",
            ),
            db=db,
            profile_id=profile_b.id,
        )

        await move_endpoint_position(
            endpoint_id=a_second.id,
            body=EndpointPositionMoveRequest(to_index=0),
            db=db,
            profile_id=profile_a.id,
        )

        endpoints_b = await list_endpoints(db=db, profile_id=profile_b.id)
        assert [endpoint.id for endpoint in endpoints_b] == [b_only.id]
        assert [endpoint.position for endpoint in endpoints_b] == [0]

        exported = await export_config(db=db, profile_id=profile_a.id)
        payload = json.loads(bytes(exported.body).decode("utf-8"))
        assert [endpoint["name"] for endpoint in payload["endpoints"]] == [
            f"DEF063 A Second {suffix}",
            f"DEF063 A First {suffix}",
        ]
        assert [endpoint["position"] for endpoint in payload["endpoints"]] == [0, 1]

        ordered_import = ConfigImportRequest.model_validate(
            {
                "version": 1,
                "vendors": [],
                "endpoints": [
                    {
                        "name": f"DEF063 Import Later {suffix}",
                        "base_url": f"https://def063-import-later.{suffix}.example.com",
                        "api_key": "sk-import-later",
                        "position": 1,
                    },
                    {
                        "name": f"DEF063 Import First {suffix}",
                        "base_url": f"https://def063-import-first.{suffix}.example.com",
                        "api_key": "sk-import-first",
                        "position": 0,
                    },
                ],
                "pricing_templates": [],
                "loadbalance_strategies": [],
                "models": [],
            }
        )

        response = await import_config(
            data=ordered_import, db=db, profile_id=profile_a.id
        )
        assert response.endpoints_imported == 2

        imported = await list_endpoints(db=db, profile_id=profile_a.id)
        assert [endpoint.name for endpoint in imported] == [
            f"DEF063 Import First {suffix}",
            f"DEF063 Import Later {suffix}",
        ]
        assert [endpoint.position for endpoint in imported] == [0, 1]

        legacy_import = ConfigImportRequest.model_validate(
            {
                "version": 1,
                "vendors": [],
                "endpoints": [
                    {
                        "name": f"DEF063 Legacy One {suffix}",
                        "base_url": f"https://def063-legacy-one.{suffix}.example.com",
                        "api_key": "sk-legacy-one",
                    },
                    {
                        "name": f"DEF063 Legacy Two {suffix}",
                        "base_url": f"https://def063-legacy-two.{suffix}.example.com",
                        "api_key": "sk-legacy-two",
                    },
                ],
                "pricing_templates": [],
                "loadbalance_strategies": [],
                "models": [],
            }
        )

        response = await import_config(
            data=legacy_import, db=db, profile_id=profile_a.id
        )
        assert response.endpoints_imported == 2

        legacy = await list_endpoints(db=db, profile_id=profile_a.id)
        assert [endpoint.name for endpoint in legacy] == [
            f"DEF063 Legacy One {suffix}",
            f"DEF063 Legacy Two {suffix}",
        ]
        assert [endpoint.position for endpoint in legacy] == [0, 1]

        untouched_b = await list_endpoints(db=db, profile_id=profile_b.id)
        assert [endpoint.id for endpoint in untouched_b] == [b_only.id]
        assert untouched_b[0].name == f"DEF063 B Only {suffix}"
