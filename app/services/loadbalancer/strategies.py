from __future__ import annotations

from typing import Literal, cast

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

from .policy import resolve_effective_loadbalance_policy
from .policy import normalize_strategy_ban_policy, validate_strategy_ban_policy
from .state import clear_strategy_state


def _validate_strategy_behavior(*, strategy_type: str, recovery_enabled: bool) -> None:
    if strategy_type == "single" and recovery_enabled:
        raise HTTPException(
            status_code=400,
            detail="single strategies must not enable failover recovery",
        )


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
    policy = resolve_effective_loadbalance_policy(strategy)
    return LoadbalanceStrategyResponse(
        id=strategy.id,
        profile_id=strategy.profile_id,
        name=strategy.name,
        strategy_type=policy.strategy_type,
        failover_recovery_enabled=policy.failover_recovery_enabled,
        failover_cooldown_seconds=int(policy.failover_cooldown_seconds),
        failover_failure_threshold=policy.failover_failure_threshold,
        failover_backoff_multiplier=policy.failover_backoff_multiplier,
        failover_max_cooldown_seconds=policy.failover_max_cooldown_seconds,
        failover_jitter_ratio=policy.failover_jitter_ratio,
        failover_auth_error_cooldown_seconds=policy.failover_auth_error_cooldown_seconds,
        failover_ban_mode=policy.failover_ban_mode,
        failover_max_cooldown_strikes_before_ban=policy.failover_max_cooldown_strikes_before_ban,
        failover_ban_duration_seconds=policy.failover_ban_duration_seconds,
        attached_model_count=attached_model_count,
        created_at=strategy.created_at,
        updated_at=strategy.updated_at,
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
    _validate_strategy_behavior(
        strategy_type=body.strategy_type,
        recovery_enabled=body.failover_recovery_enabled,
    )
    validate_strategy_ban_policy(
        strategy_type=body.strategy_type,
        failover_recovery_enabled=body.failover_recovery_enabled,
        failover_ban_mode=body.failover_ban_mode,
        failover_max_cooldown_strikes_before_ban=body.failover_max_cooldown_strikes_before_ban,
        failover_ban_duration_seconds=body.failover_ban_duration_seconds,
    )
    (
        failover_ban_mode,
        failover_max_cooldown_strikes_before_ban,
        failover_ban_duration_seconds,
    ) = normalize_strategy_ban_policy(
        strategy_type=body.strategy_type,
        failover_recovery_enabled=body.failover_recovery_enabled,
        failover_ban_mode=body.failover_ban_mode,
        failover_max_cooldown_strikes_before_ban=body.failover_max_cooldown_strikes_before_ban,
        failover_ban_duration_seconds=body.failover_ban_duration_seconds,
    )

    strategy = LoadbalanceStrategy(
        profile_id=profile_id,
        name=body.name,
        strategy_type=body.strategy_type,
        failover_recovery_enabled=body.failover_recovery_enabled,
        failover_cooldown_seconds=body.failover_cooldown_seconds,
        failover_failure_threshold=body.failover_failure_threshold,
        failover_backoff_multiplier=body.failover_backoff_multiplier,
        failover_max_cooldown_seconds=body.failover_max_cooldown_seconds,
        failover_jitter_ratio=body.failover_jitter_ratio,
        failover_auth_error_cooldown_seconds=body.failover_auth_error_cooldown_seconds,
        failover_ban_mode=failover_ban_mode,
        failover_max_cooldown_strikes_before_ban=failover_max_cooldown_strikes_before_ban,
        failover_ban_duration_seconds=failover_ban_duration_seconds,
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

    update_data: dict[str, object] = body.model_dump(exclude_unset=True)
    next_name = cast(str, update_data.get("name", strategy.name))
    next_strategy_type = cast(
        str, update_data.get("strategy_type", strategy.strategy_type)
    )
    next_recovery_enabled = (
        False
        if next_strategy_type == "single"
        else cast(
            bool,
            update_data.get(
                "failover_recovery_enabled", strategy.failover_recovery_enabled
            ),
        )
    )
    next_ban_mode = cast(
        str,
        update_data.get("failover_ban_mode", current_policy.failover_ban_mode),
    )
    next_ban_strikes = cast(
        int,
        update_data.get(
            "failover_max_cooldown_strikes_before_ban",
            current_policy.failover_max_cooldown_strikes_before_ban,
        ),
    )
    next_ban_duration = cast(
        int,
        update_data.get(
            "failover_ban_duration_seconds",
            current_policy.failover_ban_duration_seconds,
        ),
    )

    if next_name != strategy.name:
        await _ensure_unique_strategy_name(
            db,
            profile_id=profile_id,
            name=next_name,
            exclude_id=strategy.id,
        )

    _validate_strategy_behavior(
        strategy_type=next_strategy_type,
        recovery_enabled=next_recovery_enabled,
    )
    validate_strategy_ban_policy(
        strategy_type=cast(
            Literal["single", "fill-first", "round-robin", "failover"],
            next_strategy_type,
        ),
        failover_recovery_enabled=next_recovery_enabled,
        failover_ban_mode=cast(Literal["off", "temporary", "manual"], next_ban_mode),
        failover_max_cooldown_strikes_before_ban=next_ban_strikes,
        failover_ban_duration_seconds=next_ban_duration,
    )
    if next_strategy_type == "single" and (
        "strategy_type" in update_data
        or "failover_recovery_enabled" in update_data
        or strategy.failover_recovery_enabled
    ):
        update_data["failover_recovery_enabled"] = False

    if update_data:
        (
            update_data["failover_ban_mode"],
            update_data["failover_max_cooldown_strikes_before_ban"],
            update_data["failover_ban_duration_seconds"],
        ) = normalize_strategy_ban_policy(
            strategy_type=cast(
                Literal["single", "fill-first", "round-robin", "failover"],
                next_strategy_type,
            ),
            failover_recovery_enabled=next_recovery_enabled,
            failover_ban_mode=cast(
                Literal["off", "temporary", "manual"], next_ban_mode
            ),
            failover_max_cooldown_strikes_before_ban=next_ban_strikes,
            failover_ban_duration_seconds=next_ban_duration,
        )

    for key, value in update_data.items():
        setattr(strategy, key, value)

    if update_data:
        strategy.updated_at = utc_now()

    await db.flush()
    await db.refresh(strategy)
    updated_policy = resolve_effective_loadbalance_policy(strategy)

    if current_policy != updated_policy:
        await clear_strategy_state(profile_id, strategy.id)

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
