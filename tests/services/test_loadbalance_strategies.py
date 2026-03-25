from typing import cast

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.models import (
    Connection,
    Endpoint,
    LoadbalanceCurrentState,
    ModelConfig,
    Profile,
    Provider,
)
from app.routers.loadbalance import (
    create_strategy,
    delete_strategy,
    list_strategies,
    update_strategy,
)
from app.schemas.schemas import LoadbalanceStrategyCreate, LoadbalanceStrategyUpdate
from tests.loadbalance_strategy_helpers import make_loadbalance_strategy


async def _get_or_create_provider(db, *, provider_type: str = "openai"):
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

    provider = Provider(name="OpenAI strategies", provider_type=provider_type)
    db.add(provider)
    await db.flush()
    return provider


class TestLoadbalanceStrategies:
    @pytest.mark.asyncio
    async def test_strategy_crud_roundtrip(self):
        async with AsyncSessionLocal() as db:
            profile = Profile(name="Strategy CRUD Profile", is_active=False, version=0)
            db.add(profile)
            await db.flush()

            created = await create_strategy(
                body=LoadbalanceStrategyCreate(
                    name="failover-primary",
                    strategy_type="failover",
                    failover_recovery_enabled=True,
                    failover_cooldown_seconds=45,
                    failover_failure_threshold=4,
                    failover_backoff_multiplier=3.5,
                    failover_max_cooldown_seconds=720,
                    failover_jitter_ratio=0.35,
                    failover_auth_error_cooldown_seconds=2400,
                ),
                db=db,
                profile_id=profile.id,
            )
            await db.commit()

            assert created.name == "failover-primary"
            assert created.strategy_type == "failover"
            assert created.failover_recovery_enabled is True
            assert created.failover_cooldown_seconds == 45
            assert created.failover_failure_threshold == 4
            assert created.failover_backoff_multiplier == pytest.approx(3.5)
            assert created.failover_max_cooldown_seconds == 720
            assert created.failover_jitter_ratio == pytest.approx(0.35)
            assert created.failover_auth_error_cooldown_seconds == 2400
            assert created.attached_model_count == 0

            listed = await list_strategies(db=db, profile_id=profile.id)
            assert [strategy.name for strategy in listed] == ["failover-primary"]
            assert listed[0].failover_cooldown_seconds == 45
            assert listed[0].failover_failure_threshold == 4
            assert listed[0].failover_backoff_multiplier == pytest.approx(3.5)
            assert listed[0].failover_max_cooldown_seconds == 720
            assert listed[0].failover_jitter_ratio == pytest.approx(0.35)
            assert listed[0].failover_auth_error_cooldown_seconds == 2400

            updated = await update_strategy(
                strategy_id=created.id,
                body=LoadbalanceStrategyUpdate(
                    name="failover-secondary",
                    strategy_type="failover",
                    failover_recovery_enabled=True,
                    failover_cooldown_seconds=90,
                    failover_failure_threshold=5,
                    failover_backoff_multiplier=4.0,
                    failover_max_cooldown_seconds=1440,
                    failover_jitter_ratio=0.5,
                    failover_auth_error_cooldown_seconds=3600,
                ),
                db=db,
                profile_id=profile.id,
            )
            await db.commit()

            assert updated.name == "failover-secondary"
            assert updated.strategy_type == "failover"
            assert updated.failover_recovery_enabled is True
            assert updated.failover_cooldown_seconds == 90
            assert updated.failover_failure_threshold == 5
            assert updated.failover_backoff_multiplier == pytest.approx(4.0)
            assert updated.failover_max_cooldown_seconds == 1440
            assert updated.failover_jitter_ratio == pytest.approx(0.5)
            assert updated.failover_auth_error_cooldown_seconds == 3600

            deleted = await delete_strategy(
                strategy_id=created.id,
                db=db,
                profile_id=profile.id,
            )
            await db.commit()

            assert deleted == {"deleted": True}

    @pytest.mark.asyncio
    async def test_delete_strategy_rejects_attached_models(self):
        async with AsyncSessionLocal() as db:
            provider = await _get_or_create_provider(db)
            profile = Profile(
                name="Strategy Attach Profile", is_active=False, version=0
            )
            db.add(profile)
            await db.flush()

            strategy = make_loadbalance_strategy(
                profile_id=profile.id,
                strategy_type="single",
                name="single-attached",
            )
            model = ModelConfig(
                profile_id=profile.id,
                provider_id=provider.id,
                model_id="attached-model",
                model_type="native",
                loadbalance_strategy=strategy,
                is_enabled=True,
            )
            db.add_all([strategy, model])
            await db.commit()

            with pytest.raises(HTTPException) as exc_info:
                await delete_strategy(
                    strategy_id=strategy.id,
                    db=db,
                    profile_id=profile.id,
                )

            assert exc_info.value.status_code == 409
            detail = cast(dict[str, object], cast(object, exc_info.value.detail))
            assert detail["attached_model_count"] == 1

    @pytest.mark.asyncio
    async def test_updating_strategy_behavior_clears_attached_model_state(self):
        async with AsyncSessionLocal() as db:
            provider = await _get_or_create_provider(db)
            profile = Profile(name="Strategy State Profile", is_active=False, version=0)
            db.add(profile)
            await db.flush()

            strategy = make_loadbalance_strategy(
                profile_id=profile.id,
                strategy_type="failover",
                failover_recovery_enabled=True,
                name="failover-stateful",
            )
            model = ModelConfig(
                profile_id=profile.id,
                provider_id=provider.id,
                model_id="stateful-model",
                model_type="native",
                loadbalance_strategy=strategy,
                is_enabled=True,
            )
            endpoint = Endpoint(
                profile_id=profile.id,
                name="stateful-endpoint",
                base_url="https://stateful.example.com/v1",
                api_key="sk-stateful",
                position=0,
            )
            db.add_all([strategy, model, endpoint])
            await db.flush()

            connection = Connection(
                profile_id=profile.id,
                model_config_id=model.id,
                endpoint_id=endpoint.id,
                is_active=True,
                priority=0,
                name="stateful-connection",
            )
            db.add(connection)
            await db.flush()

            db.add(
                LoadbalanceCurrentState(
                    profile_id=profile.id,
                    connection_id=connection.id,
                    consecutive_failures=3,
                    last_failure_kind="timeout",
                    last_cooldown_seconds=30,
                )
            )
            await db.commit()

            updated = await update_strategy(
                strategy_id=strategy.id,
                body=LoadbalanceStrategyUpdate(
                    strategy_type="single",
                    failover_recovery_enabled=False,
                ),
                db=db,
                profile_id=profile.id,
            )
            await db.commit()

            state_rows = (
                (
                    await db.execute(
                        select(LoadbalanceCurrentState).where(
                            LoadbalanceCurrentState.profile_id == profile.id
                        )
                    )
                )
                .scalars()
                .all()
            )

            assert updated.strategy_type == "single"
            assert state_rows == []

    @pytest.mark.asyncio
    async def test_updating_strategy_failover_policy_clears_attached_model_state(self):
        async with AsyncSessionLocal() as db:
            provider = await _get_or_create_provider(db)
            profile = Profile(
                name="Strategy Policy State Profile", is_active=False, version=0
            )
            db.add(profile)
            await db.flush()

            strategy = make_loadbalance_strategy(
                profile_id=profile.id,
                strategy_type="failover",
                failover_recovery_enabled=True,
                name="failover-policy-stateful",
            )
            model = ModelConfig(
                profile_id=profile.id,
                provider_id=provider.id,
                model_id="policy-stateful-model",
                model_type="native",
                loadbalance_strategy=strategy,
                is_enabled=True,
            )
            endpoint = Endpoint(
                profile_id=profile.id,
                name="policy-stateful-endpoint",
                base_url="https://policy-stateful.example.com/v1",
                api_key="sk-policy-stateful",
                position=0,
            )
            db.add_all([strategy, model, endpoint])
            await db.flush()

            connection = Connection(
                profile_id=profile.id,
                model_config_id=model.id,
                endpoint_id=endpoint.id,
                is_active=True,
                priority=0,
                name="policy-stateful-connection",
            )
            db.add(connection)
            await db.flush()

            db.add(
                LoadbalanceCurrentState(
                    profile_id=profile.id,
                    connection_id=connection.id,
                    consecutive_failures=3,
                    last_failure_kind="timeout",
                    last_cooldown_seconds=30,
                )
            )
            await db.commit()

            updated = await update_strategy(
                strategy_id=strategy.id,
                body=LoadbalanceStrategyUpdate(
                    failover_cooldown_seconds=120,
                ),
                db=db,
                profile_id=profile.id,
            )
            await db.commit()

            state_rows = (
                (
                    await db.execute(
                        select(LoadbalanceCurrentState).where(
                            LoadbalanceCurrentState.profile_id == profile.id
                        )
                    )
                )
                .scalars()
                .all()
            )

            assert updated.failover_cooldown_seconds == 120
            assert state_rows == []
