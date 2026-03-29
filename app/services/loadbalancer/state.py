import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
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
    _ = profile_id
    _ = model_config_id
    _ = ensure_utc_datetime(now_at) or utc_now()
    return 0 if connection_count > 0 else 0


async def clear_round_robin_state_for_model(
    db: AsyncSession,
    *,
    profile_id: int,
    model_config_id: int,
) -> int:
    _ = db
    _ = profile_id
    _ = model_config_id
    return 0


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
        await session.commit()
        return cleared


async def clear_strategy_state(profile_id: int, strategy_id: int) -> int:
    async with AsyncSessionLocal() as session:
        cleared = await clear_strategy_runtime_state(
            session=session,
            profile_id=profile_id,
            strategy_id=strategy_id,
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
