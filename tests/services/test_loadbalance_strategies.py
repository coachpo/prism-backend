from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.models import (
    LoadbalanceRoundRobinState,
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
from app.services.loadbalancer.policy import canonicalize_auto_recovery_document
from tests.loadbalance_strategy_helpers import (
    make_auto_recovery_enabled,
    make_routing_policy_adaptive,
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

    vendor = Vendor(key=vendor_key, name="Dual strategy vendor")
    db.add(vendor)
    await db.flush()
    return vendor


def _strategy_public_json(strategy) -> dict[str, object]:
    return strategy.model_dump(mode="json")


def _make_strategy_create(
    *,
    name: str,
    strategy_type: str,
    legacy_strategy_type: str | None = None,
    auto_recovery: dict[str, object] | None = None,
    routing_policy: dict[str, object] | None = None,
) -> LoadbalanceStrategyCreate:
    payload: dict[str, object] = {
        "name": name,
        "strategy_type": strategy_type,
    }
    if legacy_strategy_type is not None:
        payload["legacy_strategy_type"] = legacy_strategy_type
    if auto_recovery is not None:
        payload["auto_recovery"] = auto_recovery
    if routing_policy is not None:
        payload["routing_policy"] = routing_policy
    return LoadbalanceStrategyCreate.model_validate(payload)


def _make_strategy_update(
    *,
    name: str,
    strategy_type: str,
    legacy_strategy_type: str | None = None,
    auto_recovery: dict[str, object] | None = None,
    routing_policy: dict[str, object] | None = None,
) -> LoadbalanceStrategyUpdate:
    payload: dict[str, object] = {
        "name": name,
        "strategy_type": strategy_type,
    }
    if legacy_strategy_type is not None:
        payload["legacy_strategy_type"] = legacy_strategy_type
    if auto_recovery is not None:
        payload["auto_recovery"] = auto_recovery
    if routing_policy is not None:
        payload["routing_policy"] = routing_policy
    return LoadbalanceStrategyUpdate.model_validate(payload)


def _assert_legacy_strategy_contract(
    strategy_payload: dict[str, object],
    *,
    legacy_strategy_type: str,
    auto_recovery: dict[str, object],
) -> None:
    assert strategy_payload["strategy_type"] == "legacy"
    assert strategy_payload["legacy_strategy_type"] == legacy_strategy_type
    assert strategy_payload["auto_recovery"] == canonicalize_auto_recovery_document(
        auto_recovery
    )
    assert strategy_payload.get("routing_policy") is None


def _assert_adaptive_strategy_contract(
    strategy_payload: dict[str, object],
    *,
    routing_policy: dict[str, object],
) -> None:
    assert strategy_payload["strategy_type"] == "adaptive"
    assert strategy_payload.get("legacy_strategy_type") is None
    assert strategy_payload.get("auto_recovery") is None
    assert strategy_payload["routing_policy"] == routing_policy


class TestLoadbalanceStrategies:
    def test_strategy_contract_supports_explicit_legacy_and_adaptive_strategy_types(
        self,
    ):
        legacy = _make_strategy_create(
            name="legacy-primary",
            strategy_type="legacy",
            legacy_strategy_type="single",
            auto_recovery=make_auto_recovery_enabled(),
        )
        adaptive_policy = make_routing_policy_adaptive(
            routing_objective="maximize_availability",
            deadline_budget_ms=12_000,
            hedge_enabled=True,
            hedge_delay_ms=900,
            endpoint_ping_weight=1.5,
            conversation_delay_weight=0.75,
            failure_penalty_weight=2.5,
            stale_after_seconds=180,
        )
        adaptive = _make_strategy_create(
            name="adaptive-primary",
            strategy_type="adaptive",
            routing_policy=adaptive_policy,
        )

        _assert_legacy_strategy_contract(
            _strategy_public_json(legacy),
            legacy_strategy_type="single",
            auto_recovery=make_auto_recovery_enabled(),
        )
        _assert_adaptive_strategy_contract(
            _strategy_public_json(adaptive),
            routing_policy=adaptive_policy,
        )

        with pytest.raises(ValidationError):
            _make_strategy_create(
                name="legacy-missing-mode",
                strategy_type="legacy",
                auto_recovery=make_auto_recovery_enabled(),
            )
        with pytest.raises(ValidationError):
            _make_strategy_create(
                name="adaptive-missing-policy",
                strategy_type="adaptive",
            )
        with pytest.raises(ValidationError):
            _make_strategy_create(
                name="adaptive-with-legacy-fields",
                strategy_type="adaptive",
                legacy_strategy_type="fill-first",
                auto_recovery=make_auto_recovery_enabled(),
                routing_policy=adaptive_policy,
            )

    @pytest.mark.asyncio
    async def test_strategy_crud_roundtrip_persists_dual_strategy_contract(self):
        async with AsyncSessionLocal() as db:
            profile = Profile(name="Strategy CRUD Profile", is_active=False, version=0)
            db.add(profile)
            await db.flush()

            legacy_auto_recovery = make_auto_recovery_enabled(
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
            adaptive_policy = make_routing_policy_adaptive(
                routing_objective="maximize_availability",
                deadline_budget_ms=12_000,
                hedge_enabled=True,
                hedge_delay_ms=900,
                failure_status_codes=[429, 503],
                endpoint_ping_weight=1.5,
                conversation_delay_weight=0.75,
                failure_penalty_weight=2.5,
                stale_after_seconds=180,
            )
            updated_legacy_auto_recovery = make_auto_recovery_enabled(
                status_codes=[403, 429, 503],
                base_seconds=60,
                failure_threshold=2,
                backoff_multiplier=2.0,
                max_cooldown_seconds=900,
                jitter_ratio=0.2,
            )
            updated_adaptive_policy = make_routing_policy_adaptive(
                routing_objective="minimize_latency",
                deadline_budget_ms=18_000,
                hedge_enabled=False,
                hedge_delay_ms=1_500,
                failure_status_codes=[403, 429, 503],
                endpoint_ping_weight=1.0,
                conversation_delay_weight=1.0,
                failure_penalty_weight=2.0,
                stale_after_seconds=300,
            )

            created_legacy = await create_strategy(
                body=_make_strategy_create(
                    name="legacy-fill-first",
                    strategy_type="legacy",
                    legacy_strategy_type="fill-first",
                    auto_recovery=legacy_auto_recovery,
                ),
                db=db,
                profile_id=profile.id,
            )
            created_adaptive = await create_strategy(
                body=_make_strategy_create(
                    name="adaptive-latency",
                    strategy_type="adaptive",
                    routing_policy=adaptive_policy,
                ),
                db=db,
                profile_id=profile.id,
            )
            vendor = await _get_or_create_vendor(db)
            legacy_model = ModelConfig(
                profile_id=profile.id,
                vendor_id=vendor.id,
                api_family="openai",
                model_id="legacy-round-robin-model",
                model_type="native",
                loadbalance_strategy_id=created_legacy.id,
                is_enabled=True,
            )
            db.add(legacy_model)
            await db.flush()
            db.add(
                LoadbalanceRoundRobinState(
                    profile_id=profile.id,
                    model_config_id=legacy_model.id,
                    next_cursor=1,
                )
            )
            await db.commit()

            _assert_legacy_strategy_contract(
                _strategy_public_json(created_legacy),
                legacy_strategy_type="fill-first",
                auto_recovery=legacy_auto_recovery,
            )
            _assert_adaptive_strategy_contract(
                _strategy_public_json(created_adaptive),
                routing_policy=adaptive_policy,
            )

            listed = await list_strategies(db=db, profile_id=profile.id)
            listed_by_name = {strategy.name: strategy for strategy in listed}
            _assert_legacy_strategy_contract(
                _strategy_public_json(listed_by_name["legacy-fill-first"]),
                legacy_strategy_type="fill-first",
                auto_recovery=legacy_auto_recovery,
            )
            _assert_adaptive_strategy_contract(
                _strategy_public_json(listed_by_name["adaptive-latency"]),
                routing_policy=adaptive_policy,
            )

            updated_legacy = await update_strategy(
                strategy_id=created_legacy.id,
                body=_make_strategy_update(
                    name="legacy-round-robin",
                    strategy_type="legacy",
                    legacy_strategy_type="round-robin",
                    auto_recovery=updated_legacy_auto_recovery,
                ),
                db=db,
                profile_id=profile.id,
            )
            updated_adaptive = await update_strategy(
                strategy_id=created_adaptive.id,
                body=_make_strategy_update(
                    name="adaptive-availability",
                    strategy_type="adaptive",
                    routing_policy=updated_adaptive_policy,
                ),
                db=db,
                profile_id=profile.id,
            )
            await db.commit()

            persisted_legacy = (
                await db.execute(
                    select(LoadbalanceStrategy).where(
                        LoadbalanceStrategy.id == created_legacy.id
                    )
                )
            ).scalar_one()
            persisted_adaptive = (
                await db.execute(
                    select(LoadbalanceStrategy).where(
                        LoadbalanceStrategy.id == created_adaptive.id
                    )
                )
            ).scalar_one()
            legacy_round_robin_state = (
                await db.execute(
                    select(LoadbalanceRoundRobinState).where(
                        LoadbalanceRoundRobinState.profile_id == profile.id
                    )
                )
            ).scalar_one_or_none()

            _assert_legacy_strategy_contract(
                _strategy_public_json(updated_legacy),
                legacy_strategy_type="round-robin",
                auto_recovery=updated_legacy_auto_recovery,
            )
            _assert_adaptive_strategy_contract(
                _strategy_public_json(updated_adaptive),
                routing_policy=updated_adaptive_policy,
            )
            assert persisted_legacy.strategy_type == "legacy"
            assert persisted_legacy.legacy_strategy_type == "round-robin"
            assert (
                persisted_legacy.auto_recovery
                == canonicalize_auto_recovery_document(updated_legacy_auto_recovery)
            )
            assert persisted_legacy.routing_policy is None
            assert persisted_adaptive.strategy_type == "adaptive"
            assert persisted_adaptive.legacy_strategy_type is None
            assert persisted_adaptive.auto_recovery is None
            assert persisted_adaptive.routing_policy == updated_adaptive_policy
            assert legacy_round_robin_state is None

            await db.delete(legacy_model)
            await db.commit()

            deleted_legacy = await delete_strategy(
                strategy_id=created_legacy.id,
                db=db,
                profile_id=profile.id,
            )
            deleted_adaptive = await delete_strategy(
                strategy_id=created_adaptive.id,
                db=db,
                profile_id=profile.id,
            )
            await db.commit()

            assert deleted_legacy == {"deleted": True}
            assert deleted_adaptive == {"deleted": True}

    def test_policy_resolver_supports_legacy_and_adaptive_strategy_types(self):
        from app.services.loadbalancer.policy import (
            resolve_effective_loadbalance_policy,
        )

        legacy_policy = resolve_effective_loadbalance_policy(
            SimpleNamespace(
                strategy_type="legacy",
                legacy_strategy_type="round-robin",
                auto_recovery=make_auto_recovery_enabled(
                    status_codes=[429, 503],
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
        )
        adaptive_policy = resolve_effective_loadbalance_policy(
            SimpleNamespace(
                strategy_type="adaptive",
                routing_policy=make_routing_policy_adaptive(
                    routing_objective="maximize_availability",
                    deadline_budget_ms=12_000,
                    hedge_enabled=True,
                    hedge_delay_ms=900,
                    failure_status_codes=[429, 503],
                    endpoint_ping_weight=1.5,
                    conversation_delay_weight=0.75,
                    failure_penalty_weight=2.5,
                    stale_after_seconds=180,
                ),
            )
        )

        assert legacy_policy.strategy_type == "legacy"
        assert legacy_policy.legacy_strategy_type == "round-robin"
        assert legacy_policy.failover_recovery_enabled is True
        assert legacy_policy.failover_status_codes == (429, 503)
        assert legacy_policy.failover_ban_mode == "temporary"
        assert legacy_policy.failover_ban_duration_seconds == 600

        assert adaptive_policy.strategy_type == "adaptive"
        assert adaptive_policy.legacy_strategy_type is None
        assert adaptive_policy.routing_objective == "maximize_availability"
        assert adaptive_policy.deadline_budget_ms == 12_000
        assert adaptive_policy.hedge_enabled is True
        assert adaptive_policy.monitoring_enabled is True

    def test_policy_resolver_rejects_missing_or_inconsistent_dual_strategy_shapes(self):
        from app.services.loadbalancer.policy import (
            resolve_effective_loadbalance_policy,
        )

        with pytest.raises(ValueError, match="strategy_type"):
            resolve_effective_loadbalance_policy(SimpleNamespace())
        with pytest.raises(ValueError, match="legacy_strategy_type"):
            resolve_effective_loadbalance_policy(
                SimpleNamespace(
                    strategy_type="legacy",
                    auto_recovery=make_auto_recovery_enabled(),
                )
            )
        with pytest.raises(ValueError, match="auto_recovery"):
            resolve_effective_loadbalance_policy(
                SimpleNamespace(
                    strategy_type="legacy",
                    legacy_strategy_type="single",
                )
            )

    @pytest.mark.asyncio
    async def test_delete_strategy_rejects_attached_models(self):
        async with AsyncSessionLocal() as db:
            vendor = await _get_or_create_vendor(db)
            profile = Profile(
                name="Strategy Attach Profile",
                is_active=False,
                version=0,
            )
            db.add(profile)
            await db.flush()

            strategy = LoadbalanceStrategy(
                profile_id=profile.id,
                name="legacy-attached",
                strategy_type="legacy",
                legacy_strategy_type="single",
                auto_recovery=make_auto_recovery_enabled(),
                routing_policy=None,
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
