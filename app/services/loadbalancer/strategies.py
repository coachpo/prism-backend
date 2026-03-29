from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.models.models import LoadbalanceStrategy, ModelConfig
from app.schemas.schemas import (
    LoadbalanceStrategyCreate,
    LoadbalanceStrategyResponse,
    LoadbalanceStrategyUpdate,
)

from .policy import (
    canonicalize_auto_recovery_document,
    resolve_effective_loadbalance_policy,
)
from .policy import serialize_auto_recovery
from .state import clear_strategy_state


async def _ensure_unique_strategy_name(
    db: AsyncSession,
    *,
    profile_id: int,
    name: str,
    exclude_id: int | None = None,
) -> None:
    query = select(LoadbalanceStrategy).where(
        LoadbalanceStrategy.profile_id == profile_id,
        LoadbalanceStrategy.name == name,
    )
    if exclude_id is not None:
        query = query.where(LoadbalanceStrategy.id != exclude_id)
    existing = (await db.execute(query)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="Loadbalance strategy name already exists"
        )


async def _count_attached_models(
    db: AsyncSession,
    *,
    profile_id: int,
    strategy_id: int,
) -> int:
    result = await db.execute(
        select(func.count(ModelConfig.id)).where(
            ModelConfig.profile_id == profile_id,
            ModelConfig.loadbalance_strategy_id == strategy_id,
        )
    )
    return int(result.scalar_one() or 0)


def _build_strategy_response(
    strategy: LoadbalanceStrategy,
    *,
    attached_model_count: int,
) -> LoadbalanceStrategyResponse:
    return LoadbalanceStrategyResponse.model_validate(
        {
            "id": strategy.id,
            "profile_id": strategy.profile_id,
            "name": strategy.name,
            "strategy_type": strategy.strategy_type,
            "auto_recovery": serialize_auto_recovery(
                resolve_effective_loadbalance_policy(strategy)
            ),
            "attached_model_count": attached_model_count,
            "created_at": strategy.created_at,
            "updated_at": strategy.updated_at,
        }
    )


async def list_loadbalance_strategies(
    db: AsyncSession,
    *,
    profile_id: int,
) -> list[LoadbalanceStrategyResponse]:
    result = await db.execute(
        select(LoadbalanceStrategy, func.count(ModelConfig.id))
        .outerjoin(
            ModelConfig,
            (ModelConfig.profile_id == LoadbalanceStrategy.profile_id)
            & (ModelConfig.loadbalance_strategy_id == LoadbalanceStrategy.id),
        )
        .where(LoadbalanceStrategy.profile_id == profile_id)
        .group_by(LoadbalanceStrategy.id)
        .order_by(LoadbalanceStrategy.updated_at.desc(), LoadbalanceStrategy.id.desc())
    )
    return [
        _build_strategy_response(
            strategy, attached_model_count=int(attached_model_count)
        )
        for strategy, attached_model_count in result.all()
    ]


async def create_loadbalance_strategy(
    db: AsyncSession,
    *,
    profile_id: int,
    body: LoadbalanceStrategyCreate,
) -> LoadbalanceStrategyResponse:
    await _ensure_unique_strategy_name(db, profile_id=profile_id, name=body.name)

    strategy = LoadbalanceStrategy(
        profile_id=profile_id,
        name=body.name,
        strategy_type=body.strategy_type,
        auto_recovery=canonicalize_auto_recovery_document(
            strategy_type=body.strategy_type,
            auto_recovery=body.auto_recovery,
        ),
    )
    db.add(strategy)
    await db.flush()
    await db.refresh(strategy)
    return _build_strategy_response(strategy, attached_model_count=0)


async def load_loadbalance_strategy_or_404(
    db: AsyncSession,
    *,
    profile_id: int,
    strategy_id: int,
    lock_for_update: bool = False,
) -> LoadbalanceStrategy:
    query = select(LoadbalanceStrategy).where(
        LoadbalanceStrategy.profile_id == profile_id,
        LoadbalanceStrategy.id == strategy_id,
    )
    if lock_for_update:
        query = query.with_for_update()
    strategy = (await db.execute(query)).scalar_one_or_none()
    if strategy is None:
        raise HTTPException(status_code=404, detail="Loadbalance strategy not found")
    return strategy


async def get_loadbalance_strategy(
    db: AsyncSession,
    *,
    profile_id: int,
    strategy_id: int,
) -> LoadbalanceStrategyResponse:
    strategy = await load_loadbalance_strategy_or_404(
        db,
        profile_id=profile_id,
        strategy_id=strategy_id,
    )
    attached_model_count = await _count_attached_models(
        db,
        profile_id=profile_id,
        strategy_id=strategy_id,
    )
    return _build_strategy_response(strategy, attached_model_count=attached_model_count)


async def update_loadbalance_strategy(
    db: AsyncSession,
    *,
    profile_id: int,
    strategy_id: int,
    body: LoadbalanceStrategyUpdate,
) -> LoadbalanceStrategyResponse:
    strategy = await load_loadbalance_strategy_or_404(
        db,
        profile_id=profile_id,
        strategy_id=strategy_id,
        lock_for_update=True,
    )
    current_policy = resolve_effective_loadbalance_policy(strategy)

    if body.name != strategy.name:
        await _ensure_unique_strategy_name(
            db,
            profile_id=profile_id,
            name=body.name,
            exclude_id=strategy.id,
        )

    strategy.name = body.name
    strategy.strategy_type = body.strategy_type
    strategy.auto_recovery = canonicalize_auto_recovery_document(
        strategy_type=body.strategy_type,
        auto_recovery=body.auto_recovery,
    )
    strategy.updated_at = utc_now()

    await db.flush()
    await db.refresh(strategy)
    updated_policy = resolve_effective_loadbalance_policy(strategy)

    if current_policy != updated_policy:
        _ = await clear_strategy_state(profile_id, strategy.id)

    attached_model_count = await _count_attached_models(
        db,
        profile_id=profile_id,
        strategy_id=strategy.id,
    )
    return _build_strategy_response(strategy, attached_model_count=attached_model_count)


async def delete_loadbalance_strategy(
    db: AsyncSession,
    *,
    profile_id: int,
    strategy_id: int,
) -> dict[str, bool]:
    strategy = await load_loadbalance_strategy_or_404(
        db,
        profile_id=profile_id,
        strategy_id=strategy_id,
        lock_for_update=True,
    )
    attached_model_count = await _count_attached_models(
        db,
        profile_id=profile_id,
        strategy_id=strategy.id,
    )
    if attached_model_count > 0:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Cannot delete loadbalance strategy that is attached to models",
                "attached_model_count": attached_model_count,
            },
        )

    await db.delete(strategy)
    await db.flush()
    return {"deleted": True}


__all__ = [
    "create_loadbalance_strategy",
    "delete_loadbalance_strategy",
    "get_loadbalance_strategy",
    "list_loadbalance_strategies",
    "load_loadbalance_strategy_or_404",
    "update_loadbalance_strategy",
]
