from typing import cast

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.models import (
    Connection,
    Endpoint,
    LoadbalanceCurrentState,
    LoadbalanceStrategy,
    ModelConfig,
    Profile,
    Vendor,
)
from app.routers.loadbalance import (
    create_strategy,
    delete_strategy,
    list_strategies,
    update_strategy,
)
from app.schemas.schemas import LoadbalanceStrategyCreate, LoadbalanceStrategyUpdate
from tests.loadbalance_strategy_helpers import (
    make_auto_recovery_disabled,
    make_auto_recovery_enabled,
    make_loadbalance_strategy,
)


def _vendor_key_for_api_family(api_family: str) -> str:
    return "google" if api_family == "gemini" else api_family


async def _get_or_create_vendor(db, *, api_family: str = "openai"):
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

    vendor = Vendor(key=vendor_key, name="OpenAI strategies")
    db.add(vendor)
    await db.flush()
    return vendor


def _strategy_public_json(strategy) -> dict[str, object]:
    return strategy.model_dump(mode="json")


def _strategy_auto_recovery_public_json(strategy) -> dict[str, object]:
    return cast(dict[str, object], _strategy_public_json(strategy)["auto_recovery"])


def _make_strategy_create(
    *,
    name: str,
    strategy_type: str,
    auto_recovery: object,
) -> LoadbalanceStrategyCreate:
    return LoadbalanceStrategyCreate.model_validate(
        {
            "name": name,
            "strategy_type": strategy_type,
            "auto_recovery": auto_recovery,
        }
    )


def _make_strategy_update(
    *,
    name: str,
    strategy_type: str,
    auto_recovery: object,
) -> LoadbalanceStrategyUpdate:
    return LoadbalanceStrategyUpdate.model_validate(
        {
            "name": name,
            "strategy_type": strategy_type,
            "auto_recovery": auto_recovery,
        }
    )


def _assert_no_flat_failover_fields(strategy_payload: dict[str, object]) -> None:
    assert all(
        not field_name.startswith("failover_") for field_name in strategy_payload
    )


class TestLoadbalanceStrategies:
    @pytest.mark.asyncio
    async def test_strategy_crud_roundtrip_uses_nested_auto_recovery_contract(self):
        async with AsyncSessionLocal() as db:
            profile = Profile(name="Strategy CRUD Profile", is_active=False, version=0)
            db.add(profile)
            await db.flush()

            created_auto_recovery = make_auto_recovery_enabled(
                status_codes=[503, 429],
                base_seconds=45,
                failure_threshold=4,
                backoff_multiplier=3.5,
                max_cooldown_seconds=720,
                jitter_ratio=0.35,
                ban_mode="temporary",
                max_cooldown_strikes_before_ban=3,
                ban_duration_seconds=600,
            )
            updated_auto_recovery = make_auto_recovery_disabled()

            created = await create_strategy(
                body=_make_strategy_create(
                    name="failover-primary",
                    strategy_type="failover",
                    auto_recovery=created_auto_recovery,
                ),
                db=db,
                profile_id=profile.id,
            )
            await db.commit()

            created_payload = _strategy_public_json(created)
            assert created.name == "failover-primary"
            assert created.strategy_type == "failover"
            assert created_payload["auto_recovery"] == make_auto_recovery_enabled(
                status_codes=[429, 503],
                base_seconds=45,
                failure_threshold=4,
                backoff_multiplier=3.5,
                max_cooldown_seconds=720,
                jitter_ratio=0.35,
                ban_mode="temporary",
                max_cooldown_strikes_before_ban=3,
                ban_duration_seconds=600,
            )
            assert created.attached_model_count == 0
            _assert_no_flat_failover_fields(created_payload)

            listed = await list_strategies(db=db, profile_id=profile.id)
            assert [strategy.name for strategy in listed] == ["failover-primary"]
            listed_payload = _strategy_public_json(listed[0])
            assert listed_payload["auto_recovery"] == created_payload["auto_recovery"]
            _assert_no_flat_failover_fields(listed_payload)

            updated = await update_strategy(
                strategy_id=created.id,
                body=_make_strategy_update(
                    name="failover-secondary",
                    strategy_type="failover",
                    auto_recovery=updated_auto_recovery,
                ),
                db=db,
                profile_id=profile.id,
            )
            await db.commit()

            persisted_strategy = (
                await db.execute(
                    select(LoadbalanceStrategy).where(
                        LoadbalanceStrategy.id == created.id
                    )
                )
            ).scalar_one()

            updated_payload = _strategy_public_json(updated)
            assert updated.name == "failover-secondary"
            assert updated.strategy_type == "failover"
            assert updated_payload["auto_recovery"] == updated_auto_recovery
            assert persisted_strategy.auto_recovery == updated_auto_recovery
            _assert_no_flat_failover_fields(updated_payload)

            deleted = await delete_strategy(
                strategy_id=created.id,
                db=db,
                profile_id=profile.id,
            )
            await db.commit()

            assert deleted == {"deleted": True}

    @pytest.mark.asyncio
    async def test_fill_first_strategy_crud_roundtrip(self):
        async with AsyncSessionLocal() as db:
            profile = Profile(
                name="Fill-First Strategy CRUD Profile",
                is_active=False,
                version=0,
            )
            db.add(profile)
            await db.flush()

            created_auto_recovery = make_auto_recovery_disabled()
            updated_auto_recovery = make_auto_recovery_enabled(
                status_codes=[529, 503],
                base_seconds=90,
                failure_threshold=5,
                backoff_multiplier=4.0,
                max_cooldown_seconds=1440,
                jitter_ratio=0.5,
                ban_mode="manual",
                max_cooldown_strikes_before_ban=2,
            )

            created = await create_strategy(
                body=_make_strategy_create(
                    name="fill-first-primary",
                    strategy_type="fill-first",
                    auto_recovery=created_auto_recovery,
                ),
                db=db,
                profile_id=profile.id,
            )
            await db.commit()

            assert created.name == "fill-first-primary"
            assert created.strategy_type == "fill-first"
            assert (
                _strategy_public_json(created)["auto_recovery"] == created_auto_recovery
            )

            listed = await list_strategies(db=db, profile_id=profile.id)

            assert [strategy.name for strategy in listed] == ["fill-first-primary"]
            assert listed[0].strategy_type == "fill-first"
            assert (
                _strategy_public_json(listed[0])["auto_recovery"]
                == created_auto_recovery
            )

            updated = await update_strategy(
                strategy_id=created.id,
                body=_make_strategy_update(
                    name="fill-first-secondary",
                    strategy_type="fill-first",
                    auto_recovery=updated_auto_recovery,
                ),
                db=db,
                profile_id=profile.id,
            )
            await db.commit()

            assert updated.name == "fill-first-secondary"
            assert updated.strategy_type == "fill-first"
            assert _strategy_public_json(updated)["auto_recovery"] == (
                make_auto_recovery_enabled(
                    status_codes=[503, 529],
                    base_seconds=90,
                    failure_threshold=5,
                    backoff_multiplier=4.0,
                    max_cooldown_seconds=1440,
                    jitter_ratio=0.5,
                    ban_mode="manual",
                    max_cooldown_strikes_before_ban=2,
                )
            )

    @pytest.mark.asyncio
    async def test_round_robin_strategy_crud_roundtrip(self):
        async with AsyncSessionLocal() as db:
            profile = Profile(
                name="Round-Robin Strategy CRUD Profile",
                is_active=False,
                version=0,
            )
            db.add(profile)
            await db.flush()

            created_auto_recovery = make_auto_recovery_disabled()
            updated_auto_recovery = make_auto_recovery_enabled(
                status_codes=[529, 503],
                base_seconds=90,
                failure_threshold=5,
                backoff_multiplier=4.0,
                max_cooldown_seconds=1440,
                jitter_ratio=0.5,
                ban_mode="manual",
                max_cooldown_strikes_before_ban=2,
            )

            created = await create_strategy(
                body=_make_strategy_create(
                    name="round-robin-primary",
                    strategy_type="round-robin",
                    auto_recovery=created_auto_recovery,
                ),
                db=db,
                profile_id=profile.id,
            )
            await db.commit()

            assert created.name == "round-robin-primary"
            assert created.strategy_type == "round-robin"
            assert (
                _strategy_public_json(created)["auto_recovery"] == created_auto_recovery
            )

            listed = await list_strategies(db=db, profile_id=profile.id)

            assert [strategy.name for strategy in listed] == ["round-robin-primary"]
            assert listed[0].strategy_type == "round-robin"
            assert (
                _strategy_public_json(listed[0])["auto_recovery"]
                == created_auto_recovery
            )

            updated = await update_strategy(
                strategy_id=created.id,
                body=_make_strategy_update(
                    name="round-robin-secondary",
                    strategy_type="round-robin",
                    auto_recovery=updated_auto_recovery,
                ),
                db=db,
                profile_id=profile.id,
            )
            await db.commit()

            assert updated.name == "round-robin-secondary"
            assert updated.strategy_type == "round-robin"
            assert _strategy_public_json(updated)["auto_recovery"] == (
                make_auto_recovery_enabled(
                    status_codes=[503, 529],
                    base_seconds=90,
                    failure_threshold=5,
                    backoff_multiplier=4.0,
                    max_cooldown_seconds=1440,
                    jitter_ratio=0.5,
                    ban_mode="manual",
                    max_cooldown_strikes_before_ban=2,
                )
            )

    def test_fill_first_strategy_allows_recovery_fields_while_single_still_rejects_them(
        self,
    ):
        with pytest.raises(ValidationError):
            _ = _make_strategy_create(
                name="single-with-recovery",
                strategy_type="single",
                auto_recovery=make_auto_recovery_enabled(),
            )

        created = _make_strategy_create(
            name="fill-first-with-recovery",
            strategy_type="fill-first",
            auto_recovery=make_auto_recovery_enabled(
                status_codes=[503, 429],
                base_seconds=45,
                failure_threshold=4,
                backoff_multiplier=3.5,
                max_cooldown_seconds=720,
                jitter_ratio=0.35,
                ban_mode="temporary",
                max_cooldown_strikes_before_ban=3,
                ban_duration_seconds=600,
            ),
        )

        assert created.strategy_type == "fill-first"
        assert _strategy_public_json(created)["auto_recovery"] == (
            make_auto_recovery_enabled(
                status_codes=[429, 503],
                base_seconds=45,
                failure_threshold=4,
                backoff_multiplier=3.5,
                max_cooldown_seconds=720,
                jitter_ratio=0.35,
                ban_mode="temporary",
                max_cooldown_strikes_before_ban=3,
                ban_duration_seconds=600,
            )
        )

    def test_strategy_contract_sorts_status_codes_and_rejects_invalid_lists(self):
        created = _make_strategy_create(
            name="failover-status-codes",
            strategy_type="failover",
            auto_recovery=make_auto_recovery_enabled(status_codes=[503, 429, 504]),
        )

        assert _strategy_auto_recovery_public_json(created)["status_codes"] == [
            429,
            503,
            504,
        ]

        with pytest.raises(ValidationError):
            _ = _make_strategy_create(
                name="duplicate-status-codes",
                strategy_type="failover",
                auto_recovery=make_auto_recovery_enabled(status_codes=[429, 429]),
            )

        with pytest.raises(ValidationError):
            _ = _make_strategy_create(
                name="out-of-range-status-codes",
                strategy_type="failover",
                auto_recovery=make_auto_recovery_enabled(status_codes=[99, 429]),
            )

    def test_strategy_contract_rejects_unrecognized_cooldown_fields(self):
        invalid_auto_recovery = cast(
            dict[str, object], cast(object, make_auto_recovery_enabled())
        )
        cooldown = cast(dict[str, object], invalid_auto_recovery["cooldown"])
        invalid_auto_recovery["cooldown"] = {
            **cooldown,
            "unexpected_cooldown_seconds": 2400,
        }

        with pytest.raises(ValidationError):
            _ = LoadbalanceStrategyCreate.model_validate(
                {
                    "name": "unexpected-cooldown-field",
                    "strategy_type": "failover",
                    "auto_recovery": invalid_auto_recovery,
                }
            )

    @pytest.mark.asyncio
    async def test_delete_strategy_rejects_attached_models(self):
        async with AsyncSessionLocal() as db:
            vendor = await _get_or_create_vendor(db)
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
                vendor_id=vendor.id,
                api_family="openai",
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
            vendor = await _get_or_create_vendor(db)
            profile = Profile(name="Strategy State Profile", is_active=False, version=0)
            db.add(profile)
            await db.flush()

            strategy = make_loadbalance_strategy(
                profile_id=profile.id,
                strategy_type="failover",
                auto_recovery=make_auto_recovery_enabled(),
                name="failover-stateful",
            )
            model = ModelConfig(
                profile_id=profile.id,
                vendor_id=vendor.id,
                api_family="openai",
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
                body=_make_strategy_update(
                    name=strategy.name,
                    strategy_type="single",
                    auto_recovery=make_auto_recovery_disabled(),
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
            vendor = await _get_or_create_vendor(db)
            profile = Profile(
                name="Strategy Policy State Profile", is_active=False, version=0
            )
            db.add(profile)
            await db.flush()

            strategy = make_loadbalance_strategy(
                profile_id=profile.id,
                strategy_type="failover",
                auto_recovery=make_auto_recovery_enabled(),
                name="failover-policy-stateful",
            )
            model = ModelConfig(
                profile_id=profile.id,
                vendor_id=vendor.id,
                api_family="openai",
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

            replacement_auto_recovery = make_auto_recovery_enabled(
                status_codes=[529, 503],
                base_seconds=120,
                failure_threshold=5,
                backoff_multiplier=4.0,
                max_cooldown_seconds=1440,
                jitter_ratio=0.5,
                ban_mode="manual",
                max_cooldown_strikes_before_ban=2,
            )

            updated = await update_strategy(
                strategy_id=strategy.id,
                body=_make_strategy_update(
                    name=strategy.name,
                    strategy_type="failover",
                    auto_recovery=replacement_auto_recovery,
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

            assert _strategy_public_json(updated)["auto_recovery"] == (
                make_auto_recovery_enabled(
                    status_codes=[503, 529],
                    base_seconds=120,
                    failure_threshold=5,
                    backoff_multiplier=4.0,
                    max_cooldown_seconds=1440,
                    jitter_ratio=0.5,
                    ban_mode="manual",
                    max_cooldown_strikes_before_ban=2,
                )
            )
            assert state_rows == []

    @pytest.mark.asyncio
    async def test_switching_strategy_from_failover_to_fill_first_clears_attached_model_state(
        self,
    ):
        async with AsyncSessionLocal() as db:
            vendor = await _get_or_create_vendor(db)
            profile = Profile(
                name="Strategy Fill-First State Profile",
                is_active=False,
                version=0,
            )
            db.add(profile)
            await db.flush()

            strategy = make_loadbalance_strategy(
                profile_id=profile.id,
                strategy_type="failover",
                auto_recovery=make_auto_recovery_enabled(),
                name="fill-first-stateful",
            )
            model = ModelConfig(
                profile_id=profile.id,
                vendor_id=vendor.id,
                api_family="openai",
                model_id="fill-first-stateful-model",
                model_type="native",
                loadbalance_strategy=strategy,
                is_enabled=True,
            )
            endpoint = Endpoint(
                profile_id=profile.id,
                name="fill-first-stateful-endpoint",
                base_url="https://fill-first-stateful.example.com/v1",
                api_key="sk-fill-first-stateful",
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
                name="fill-first-stateful-connection",
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

            replacement_auto_recovery = make_auto_recovery_enabled(
                status_codes=[529, 503],
                base_seconds=90,
                failure_threshold=5,
                backoff_multiplier=4.0,
                max_cooldown_seconds=1440,
                jitter_ratio=0.5,
                ban_mode="manual",
                max_cooldown_strikes_before_ban=2,
            )

            updated = await update_strategy(
                strategy_id=strategy.id,
                body=_make_strategy_update(
                    name=strategy.name,
                    strategy_type="fill-first",
                    auto_recovery=replacement_auto_recovery,
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

            assert updated.strategy_type == "fill-first"
            assert _strategy_public_json(updated)["auto_recovery"] == (
                make_auto_recovery_enabled(
                    status_codes=[503, 529],
                    base_seconds=90,
                    failure_threshold=5,
                    backoff_multiplier=4.0,
                    max_cooldown_seconds=1440,
                    jitter_ratio=0.5,
                    ban_mode="manual",
                    max_cooldown_strikes_before_ban=2,
                )
            )
            assert state_rows == []

    def test_strategy_ban_policy_validation_rejects_invalid_combinations(self):
        invalid_ban_off = cast(
            dict[str, object], cast(object, make_auto_recovery_enabled())
        )
        invalid_ban_off["ban"] = {
            "mode": "off",
            "max_cooldown_strikes_before_ban": 1,
            "ban_duration_seconds": 60,
        }

        with pytest.raises(ValidationError):
            _ = LoadbalanceStrategyCreate.model_validate(
                {
                    "name": "invalid-ban-off",
                    "strategy_type": "failover",
                    "auto_recovery": invalid_ban_off,
                }
            )

        invalid_ban_temporary = cast(
            dict[str, object], cast(object, make_auto_recovery_enabled())
        )
        invalid_ban_temporary["ban"] = {
            "mode": "temporary",
            "max_cooldown_strikes_before_ban": 1,
            "ban_duration_seconds": 0,
        }

        with pytest.raises(ValidationError):
            _ = LoadbalanceStrategyCreate.model_validate(
                {
                    "name": "invalid-ban-temporary",
                    "strategy_type": "failover",
                    "auto_recovery": invalid_ban_temporary,
                }
            )

        invalid_ban_manual = cast(
            dict[str, object], cast(object, make_auto_recovery_enabled())
        )
        invalid_ban_manual["ban"] = {
            "mode": "manual",
            "max_cooldown_strikes_before_ban": 1,
            "ban_duration_seconds": 60,
        }

        with pytest.raises(ValidationError):
            _ = LoadbalanceStrategyCreate.model_validate(
                {
                    "name": "invalid-ban-manual",
                    "strategy_type": "failover",
                    "auto_recovery": invalid_ban_manual,
                }
            )

    @pytest.mark.asyncio
    async def test_updating_strategy_ban_policy_clears_attached_model_state(self):
        async with AsyncSessionLocal() as db:
            vendor = await _get_or_create_vendor(db)
            profile = Profile(
                name="Strategy Ban Policy State Profile", is_active=False, version=0
            )
            db.add(profile)
            await db.flush()

            strategy = make_loadbalance_strategy(
                profile_id=profile.id,
                strategy_type="failover",
                auto_recovery=make_auto_recovery_enabled(),
                name="failover-ban-policy-stateful",
            )
            model = ModelConfig(
                profile_id=profile.id,
                vendor_id=vendor.id,
                api_family="openai",
                model_id="ban-policy-stateful-model",
                model_type="native",
                loadbalance_strategy=strategy,
                is_enabled=True,
            )
            endpoint = Endpoint(
                profile_id=profile.id,
                name="ban-policy-stateful-endpoint",
                base_url="https://ban-policy-stateful.example.com/v1",
                api_key="sk-ban-policy-stateful",
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
                name="ban-policy-stateful-connection",
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

            replacement_auto_recovery = make_auto_recovery_enabled(
                status_codes=[529, 503],
                base_seconds=90,
                failure_threshold=5,
                backoff_multiplier=4.0,
                max_cooldown_seconds=1440,
                jitter_ratio=0.5,
                ban_mode="temporary",
                max_cooldown_strikes_before_ban=2,
                ban_duration_seconds=600,
            )

            updated = await update_strategy(
                strategy_id=strategy.id,
                body=_make_strategy_update(
                    name=strategy.name,
                    strategy_type="failover",
                    auto_recovery=replacement_auto_recovery,
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

            assert _strategy_public_json(updated)["auto_recovery"] == (
                make_auto_recovery_enabled(
                    status_codes=[503, 529],
                    base_seconds=90,
                    failure_threshold=5,
                    backoff_multiplier=4.0,
                    max_cooldown_seconds=1440,
                    jitter_ratio=0.5,
                    ban_mode="temporary",
                    max_cooldown_strikes_before_ban=2,
                    ban_duration_seconds=600,
                )
            )
            assert state_rows == []
