from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.models import (
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
from tests.loadbalance_strategy_helpers import make_routing_policy_adaptive


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

    vendor = Vendor(key=vendor_key, name="Adaptive routing vendor")
    db.add(vendor)
    await db.flush()
    return vendor


def _strategy_public_json(strategy) -> dict[str, object]:
    return strategy.model_dump(mode="json")


def _make_strategy_create(
    *,
    name: str,
    routing_policy: dict[str, object],
) -> LoadbalanceStrategyCreate:
    return LoadbalanceStrategyCreate.model_validate(
        {
            "name": name,
            "routing_policy": routing_policy,
        }
    )


def _make_strategy_update(
    *,
    name: str,
    routing_policy: dict[str, object],
) -> LoadbalanceStrategyUpdate:
    return LoadbalanceStrategyUpdate.model_validate(
        {
            "name": name,
            "routing_policy": routing_policy,
        }
    )


def _assert_routing_policy_contract(strategy_payload: dict[str, object]) -> None:
    assert "routing_policy" in strategy_payload
    assert "strategy_type" not in strategy_payload
    assert "auto_recovery" not in strategy_payload


class TestLoadbalanceStrategies:
    def test_strategy_contract_uses_routing_policy_document_and_rejects_legacy_fields(
        self,
    ):
        created = _make_strategy_create(
            name="adaptive-primary",
            routing_policy=make_routing_policy_adaptive(),
        )

        created_payload = _strategy_public_json(created)
        _assert_routing_policy_contract(created_payload)
        assert created_payload["routing_policy"] == make_routing_policy_adaptive()

        with pytest.raises(ValidationError):
            _ = LoadbalanceStrategyCreate.model_validate(
                {
                    "name": "legacy-failover",
                    "strategy_type": "failover",
                    "auto_recovery": {"mode": "disabled"},
                }
            )

    @pytest.mark.asyncio
    async def test_strategy_crud_roundtrip_persists_single_routing_policy_document(
        self,
    ):
        async with AsyncSessionLocal() as db:
            profile = Profile(name="Strategy CRUD Profile", is_active=False, version=0)
            db.add(profile)
            await db.flush()

            created_policy = make_routing_policy_adaptive(
                endpoint_ping_weight=0.75,
                conversation_delay_weight=1.5,
                failure_penalty_weight=3.0,
                stale_after_seconds=180,
            )
            updated_policy = make_routing_policy_adaptive(
                routing_objective="maximize_availability",
                deadline_budget_ms=18_000,
                hedge_enabled=True,
                hedge_delay_ms=900,
                endpoint_ping_weight=0.5,
                conversation_delay_weight=0.75,
                failure_penalty_weight=4.0,
                stale_after_seconds=120,
            )

            created = await create_strategy(
                body=_make_strategy_create(
                    name="adaptive-primary",
                    routing_policy=created_policy,
                ),
                db=db,
                profile_id=profile.id,
            )
            await db.commit()

            created_payload = _strategy_public_json(created)
            assert created.name == "adaptive-primary"
            assert created_payload["routing_policy"] == created_policy
            assert created.attached_model_count == 0
            _assert_routing_policy_contract(created_payload)

            listed = await list_strategies(db=db, profile_id=profile.id)
            assert [strategy.name for strategy in listed] == ["adaptive-primary"]
            listed_payload = _strategy_public_json(listed[0])
            assert listed_payload["routing_policy"] == created_policy
            _assert_routing_policy_contract(listed_payload)

            updated = await update_strategy(
                strategy_id=created.id,
                body=_make_strategy_update(
                    name="adaptive-secondary",
                    routing_policy=updated_policy,
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
            assert updated.name == "adaptive-secondary"
            assert updated_payload["routing_policy"] == updated_policy
            assert getattr(persisted_strategy, "routing_policy") == updated_policy
            _assert_routing_policy_contract(updated_payload)

            deleted = await delete_strategy(
                strategy_id=created.id,
                db=db,
                profile_id=profile.id,
            )
            await db.commit()

            assert deleted == {"deleted": True}

    def test_policy_resolver_exposes_monitoring_inputs_from_routing_policy_document(
        self,
    ):
        from app.services.loadbalancer.policy import (
            resolve_effective_loadbalance_policy,
        )

        strategy = SimpleNamespace(
            routing_policy=make_routing_policy_adaptive(
                deadline_budget_ms=22_000,
                hedge_enabled=True,
                hedge_delay_ms=750,
                endpoint_ping_weight=0.6,
                conversation_delay_weight=1.4,
                failure_penalty_weight=2.5,
                stale_after_seconds=90,
            )
        )

        policy = resolve_effective_loadbalance_policy(strategy)

        assert getattr(policy, "kind", None) == "adaptive"
        assert getattr(policy, "routing_objective", None) == "minimize_latency"
        assert getattr(policy, "deadline_budget_ms", None) == 22_000
        assert getattr(policy, "hedge_enabled", None) is True
        assert getattr(policy, "hedge_delay_ms", None) == 750
        assert getattr(policy, "monitoring_enabled", None) is True
        assert getattr(policy, "monitoring_stale_after_seconds", None) == 90
        assert getattr(policy, "monitoring_endpoint_ping_weight", None) == 0.6
        assert getattr(policy, "monitoring_conversation_delay_weight", None) == 1.4
        assert getattr(policy, "monitoring_failure_penalty_weight", None) == 2.5

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
                name="adaptive-attached",
                routing_policy=make_routing_policy_adaptive(),
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
