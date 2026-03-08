import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
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
async def test_connection_priority_move_respects_profile_and_model_ownership():
    from app.core.database import AsyncSessionLocal, get_engine
    from app.models.models import Endpoint, ModelConfig, Profile
    from app.routers.connections import create_connection, move_connection_priority
    from app.schemas.schemas import ConnectionCreate, ConnectionPriorityMoveRequest

    await get_engine().dispose()

    suffix = _unique_suffix()

    async with AsyncSessionLocal() as db:
        provider = await _get_or_create_provider(db)

        profile_a = Profile(
            name=f"DEF069 Profile A {suffix}",
            is_active=False,
            is_default=False,
            is_editable=True,
            version=0,
        )
        profile_b = Profile(
            name=f"DEF069 Profile B {suffix}",
            is_active=False,
            is_default=False,
            is_editable=True,
            version=0,
        )
        db.add_all([profile_a, profile_b])
        await db.flush()

        model_a = ModelConfig(
            profile_id=profile_a.id,
            provider_id=provider.id,
            model_id=f"def069-model-a-{suffix}",
            model_type="native",
            lb_strategy="single",
            failover_recovery_enabled=True,
            failover_recovery_cooldown_seconds=60,
            is_enabled=True,
        )
        model_b = ModelConfig(
            profile_id=profile_b.id,
            provider_id=provider.id,
            model_id=f"def069-model-b-{suffix}",
            model_type="native",
            lb_strategy="single",
            failover_recovery_enabled=True,
            failover_recovery_cooldown_seconds=60,
            is_enabled=True,
        )
        db.add_all([model_a, model_b])
        await db.flush()

        endpoint_a = Endpoint(
            profile_id=profile_a.id,
            name=f"DEF069 Endpoint A {suffix}",
            base_url=f"https://def069-a.{suffix}.example.com",
            api_key="sk-def069-a",
            position=0,
        )
        endpoint_b = Endpoint(
            profile_id=profile_b.id,
            name=f"DEF069 Endpoint B {suffix}",
            base_url=f"https://def069-b.{suffix}.example.com",
            api_key="sk-def069-b",
            position=0,
        )
        db.add_all([endpoint_a, endpoint_b])
        await db.flush()

        connection_a = await create_connection(
            model_config_id=model_a.id,
            body=ConnectionCreate(
                endpoint_id=endpoint_a.id, name="Profile A connection"
            ),
            db=db,
            profile_id=profile_a.id,
        )
        connection_b = await create_connection(
            model_config_id=model_b.id,
            body=ConnectionCreate(
                endpoint_id=endpoint_b.id, name="Profile B connection"
            ),
            db=db,
            profile_id=profile_b.id,
        )

        with pytest.raises(HTTPException) as wrong_model:
            await move_connection_priority(
                model_config_id=model_b.id,
                connection_id=connection_a.id,
                body=ConnectionPriorityMoveRequest(to_index=0),
                db=db,
                profile_id=profile_a.id,
            )
        assert wrong_model.value.status_code == 404

        with pytest.raises(HTTPException) as wrong_profile:
            await move_connection_priority(
                model_config_id=model_a.id,
                connection_id=connection_b.id,
                body=ConnectionPriorityMoveRequest(to_index=0),
                db=db,
                profile_id=profile_a.id,
            )
        assert wrong_profile.value.status_code == 404
