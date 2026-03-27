import logging
from datetime import datetime
from typing import cast

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.time import ensure_utc_datetime, utc_now
from app.core.database import AsyncSessionLocal
from app.models.models import (
    Connection,
    LoadbalanceCurrentState,
    LoadbalanceRoundRobinState,
    ModelConfig,
)

from .policy import BanMode
from .types import FailureKind, RecoveryStateEntry

LOGGER_NAME = "app.services.loadbalancer"
logger = logging.getLogger(LOGGER_NAME)


def get_loadbalancer_settings():
    return get_settings()


def current_state_to_recovery_entry(
    current_state: LoadbalanceCurrentState,
) -> RecoveryStateEntry:
    return {
        "consecutive_failures": current_state.consecutive_failures,
        "blocked_until_at": current_state.blocked_until_at,
        "max_cooldown_strikes": current_state.max_cooldown_strikes,
        "ban_mode": cast(BanMode, current_state.ban_mode),
        "banned_until_at": ensure_utc_datetime(current_state.banned_until_at),
        "last_cooldown_seconds": float(current_state.last_cooldown_seconds),
        "last_failure_kind": cast(FailureKind | None, current_state.last_failure_kind),
        "probe_eligible_logged": current_state.probe_eligible_logged,
    }


async def get_current_states_for_connections(
    db: AsyncSession,
    *,
    profile_id: int,
    connection_ids: list[int],
) -> dict[int, LoadbalanceCurrentState]:
    if not connection_ids:
        return {}

    result = await db.execute(
        select(LoadbalanceCurrentState).where(
            LoadbalanceCurrentState.profile_id == profile_id,
            LoadbalanceCurrentState.connection_id.in_(connection_ids),
        )
    )
    rows = list(result.scalars().all())
    return {row.connection_id: row for row in rows}


async def list_current_states_for_model(
    db: AsyncSession,
    *,
    profile_id: int,
    model_config_id: int,
) -> list[LoadbalanceCurrentState]:
    result = await db.execute(
        select(LoadbalanceCurrentState)
        .join(Connection, Connection.id == LoadbalanceCurrentState.connection_id)
        .where(
            LoadbalanceCurrentState.profile_id == profile_id,
            Connection.profile_id == profile_id,
            Connection.model_config_id == model_config_id,
        )
        .order_by(Connection.priority.asc(), Connection.id.asc())
    )
    return list(result.scalars().all())


async def claim_round_robin_cursor_position(
    *,
    profile_id: int,
    model_config_id: int,
    connection_count: int,
    now_at: datetime | None = None,
) -> int:
    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    normalized_connection_count = max(connection_count, 1)

    async with AsyncSessionLocal() as session:
        await session.execute(
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
        result = await session.execute(
            select(LoadbalanceRoundRobinState)
            .where(
                LoadbalanceRoundRobinState.profile_id == profile_id,
                LoadbalanceRoundRobinState.model_config_id == model_config_id,
            )
            .with_for_update()
        )
        round_robin_state = result.scalar_one()
        current_cursor = round_robin_state.next_cursor % normalized_connection_count
        round_robin_state.next_cursor = (
            current_cursor + 1
        ) % normalized_connection_count
        round_robin_state.updated_at = normalized_now
        await session.commit()
        return current_cursor


async def clear_round_robin_state_for_model(
    db: AsyncSession,
    *,
    profile_id: int,
    model_config_id: int,
) -> int:
    result = await db.execute(
        delete(LoadbalanceRoundRobinState).where(
            LoadbalanceRoundRobinState.profile_id == profile_id,
            LoadbalanceRoundRobinState.model_config_id == model_config_id,
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def clear_connection_state(profile_id: int, connection_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            delete(LoadbalanceCurrentState).where(
                LoadbalanceCurrentState.profile_id == profile_id,
                LoadbalanceCurrentState.connection_id == connection_id,
            )
        )
        await session.commit()
        return bool(getattr(result, "rowcount", 0))


async def clear_connection_states(
    profile_id: int,
    connection_ids: list[int],
) -> int:
    if not connection_ids:
        return 0

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            delete(LoadbalanceCurrentState).where(
                LoadbalanceCurrentState.profile_id == profile_id,
                LoadbalanceCurrentState.connection_id.in_(connection_ids),
            )
        )
        await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)


async def clear_model_state(profile_id: int, model_config_id: int) -> int:
    async with AsyncSessionLocal() as session:
        connection_ids = list(
            (
                await session.execute(
                    select(Connection.id).where(
                        Connection.profile_id == profile_id,
                        Connection.model_config_id == model_config_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        deleted_current_state = 0
        if connection_ids:
            result = await session.execute(
                delete(LoadbalanceCurrentState).where(
                    LoadbalanceCurrentState.profile_id == profile_id,
                    LoadbalanceCurrentState.connection_id.in_(connection_ids),
                )
            )
            deleted_current_state = int(getattr(result, "rowcount", 0) or 0)

        round_robin_result = await session.execute(
            delete(LoadbalanceRoundRobinState).where(
                LoadbalanceRoundRobinState.profile_id == profile_id,
                LoadbalanceRoundRobinState.model_config_id == model_config_id,
            )
        )
        await session.commit()
        return deleted_current_state + int(
            getattr(round_robin_result, "rowcount", 0) or 0
        )


async def clear_strategy_state(profile_id: int, strategy_id: int) -> int:
    async with AsyncSessionLocal() as session:
        model_config_ids = list(
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
        connection_ids = list(
            (
                await session.execute(
                    select(Connection.id)
                    .join(ModelConfig, ModelConfig.id == Connection.model_config_id)
                    .where(
                        Connection.profile_id == profile_id,
                        ModelConfig.profile_id == profile_id,
                        ModelConfig.loadbalance_strategy_id == strategy_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        deleted_current_state = 0
        if connection_ids:
            result = await session.execute(
                delete(LoadbalanceCurrentState).where(
                    LoadbalanceCurrentState.profile_id == profile_id,
                    LoadbalanceCurrentState.connection_id.in_(connection_ids),
                )
            )
            deleted_current_state = int(getattr(result, "rowcount", 0) or 0)

        deleted_round_robin_state = 0
        if model_config_ids:
            round_robin_result = await session.execute(
                delete(LoadbalanceRoundRobinState).where(
                    LoadbalanceRoundRobinState.profile_id == profile_id,
                    LoadbalanceRoundRobinState.model_config_id.in_(model_config_ids),
                )
            )
            deleted_round_robin_state = int(
                getattr(round_robin_result, "rowcount", 0) or 0
            )

        if not connection_ids and not model_config_ids:
            await session.rollback()
            return 0

        await session.commit()
        return deleted_current_state + deleted_round_robin_state


async def clear_profile_state(profile_id: int) -> int:
    async with AsyncSessionLocal() as session:
        current_state_result = await session.execute(
            delete(LoadbalanceCurrentState).where(
                LoadbalanceCurrentState.profile_id == profile_id,
            )
        )
        round_robin_result = await session.execute(
            delete(LoadbalanceRoundRobinState).where(
                LoadbalanceRoundRobinState.profile_id == profile_id,
            )
        )
        await session.commit()
        return int(getattr(current_state_result, "rowcount", 0) or 0) + int(
            getattr(round_robin_result, "rowcount", 0) or 0
        )


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
