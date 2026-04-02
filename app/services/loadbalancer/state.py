import logging
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.models.models import LoadbalanceRoundRobinState, ModelConfig
from app.core.time import ensure_utc_datetime, utc_now
from app.models.models import RoutingConnectionRuntimeState

from .runtime_store import (
    clear_connection_runtime_state,
    clear_connection_runtime_states,
    clear_model_runtime_state,
    clear_profile_runtime_state,
    clear_strategy_runtime_state,
    get_runtime_states_for_connections,
    list_runtime_states_for_model,
    runtime_state_to_recovery_entry,
)
from .types import FailureKind, RecoveryStateEntry

LOGGER_NAME = "app.services.loadbalancer"
logger = logging.getLogger(LOGGER_NAME)


def get_loadbalancer_settings():
    return get_settings()


def current_state_to_recovery_entry(
    current_state: RoutingConnectionRuntimeState,
) -> RecoveryStateEntry:
    return runtime_state_to_recovery_entry(current_state)


async def get_current_states_for_connections(
    db: AsyncSession,
    *,
    profile_id: int,
    connection_ids: list[int],
) -> dict[int, RoutingConnectionRuntimeState]:
    return await get_runtime_states_for_connections(
        db,
        profile_id=profile_id,
        connection_ids=connection_ids,
    )


async def list_current_states_for_model(
    db: AsyncSession,
    *,
    profile_id: int,
    model_config_id: int,
) -> list[RoutingConnectionRuntimeState]:
    return await list_runtime_states_for_model(
        db,
        profile_id=profile_id,
        model_config_id=model_config_id,
    )


async def claim_round_robin_cursor_position(
    *,
    profile_id: int,
    model_config_id: int,
    connection_count: int,
    now_at: datetime | None = None,
) -> int:
    if connection_count <= 0:
        return 0

    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    async with AsyncSessionLocal() as session:
        _ = await session.execute(
            insert(LoadbalanceRoundRobinState)
            .values(
                profile_id=profile_id,
                model_config_id=model_config_id,
                next_cursor=0,
                created_at=normalized_now,
                updated_at=normalized_now,
            )
            .on_conflict_do_nothing(index_elements=["profile_id", "model_config_id"])
        )
        state_row = (
            await session.execute(
                select(LoadbalanceRoundRobinState)
                .where(
                    LoadbalanceRoundRobinState.profile_id == profile_id,
                    LoadbalanceRoundRobinState.model_config_id == model_config_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        cursor = state_row.next_cursor % connection_count
        state_row.next_cursor = (cursor + 1) % connection_count
        state_row.updated_at = normalized_now
        await session.commit()
        return cursor


async def clear_round_robin_state_for_model(
    db: AsyncSession,
    *,
    profile_id: int,
    model_config_id: int,
) -> int:
    if db is not None:
        result = await db.execute(
            delete(LoadbalanceRoundRobinState).where(
                LoadbalanceRoundRobinState.profile_id == profile_id,
                LoadbalanceRoundRobinState.model_config_id == model_config_id,
            )
        )
        return int(result.rowcount or 0)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            delete(LoadbalanceRoundRobinState).where(
                LoadbalanceRoundRobinState.profile_id == profile_id,
                LoadbalanceRoundRobinState.model_config_id == model_config_id,
            )
        )
        await session.commit()
        return int(result.rowcount or 0)


async def clear_connection_state(profile_id: int, connection_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        cleared = await clear_connection_runtime_state(
            session=session,
            profile_id=profile_id,
            connection_id=connection_id,
        )
        await session.commit()
        return cleared > 0


async def clear_connection_states(
    profile_id: int,
    connection_ids: list[int],
) -> int:
    async with AsyncSessionLocal() as session:
        cleared = await clear_connection_runtime_states(
            session=session,
            profile_id=profile_id,
            connection_ids=connection_ids,
        )
        await session.commit()
        return cleared


async def clear_model_state(profile_id: int, model_config_id: int) -> int:
    async with AsyncSessionLocal() as session:
        cleared = await clear_model_runtime_state(
            session=session,
            profile_id=profile_id,
            model_config_id=model_config_id,
        )
        _ = await session.execute(
            delete(LoadbalanceRoundRobinState).where(
                LoadbalanceRoundRobinState.profile_id == profile_id,
                LoadbalanceRoundRobinState.model_config_id == model_config_id,
            )
        )
        await session.commit()
        return cleared


async def clear_strategy_state(profile_id: int, strategy_id: int) -> int:
    async with AsyncSessionLocal() as session:
        cleared = await clear_strategy_runtime_state(
            session=session,
            profile_id=profile_id,
            strategy_id=strategy_id,
        )
        strategy_model_ids = list(
            (
                await session.execute(
                    select(ModelConfig.id).where(
                        ModelConfig.profile_id == profile_id,
                        ModelConfig.loadbalance_strategy_id == strategy_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if strategy_model_ids:
            _ = await session.execute(
                delete(LoadbalanceRoundRobinState).where(
                    LoadbalanceRoundRobinState.profile_id == profile_id,
                    LoadbalanceRoundRobinState.model_config_id.in_(strategy_model_ids),
                )
            )
        await session.commit()
        return cleared


async def clear_profile_state(profile_id: int) -> int:
    async with AsyncSessionLocal() as session:
        cleared = await clear_profile_runtime_state(
            session=session,
            profile_id=profile_id,
        )
        await session.commit()
        return cleared


__all__ = [
    "clear_connection_state",
    "clear_connection_states",
    "clear_model_state",
    "clear_round_robin_state_for_model",
    "clear_strategy_state",
    "clear_profile_state",
    "claim_round_robin_cursor_position",
    "FailureKind",
    "LOGGER_NAME",
    "RecoveryStateEntry",
    "current_state_to_recovery_entry",
    "get_current_states_for_connections",
    "get_loadbalancer_settings",
    "list_current_states_for_model",
    "logger",
]
