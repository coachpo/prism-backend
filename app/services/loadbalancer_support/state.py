import logging
from datetime import datetime
from typing import Literal, TypedDict, cast

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.time import ensure_utc_datetime, utc_now
from app.core.database import AsyncSessionLocal
from app.models.models import Connection, LoadbalanceCurrentState

LOGGER_NAME = "app.services.loadbalancer"
logger = logging.getLogger(LOGGER_NAME)

FailureKind = Literal["transient_http", "auth_like", "connect_error", "timeout"]


class RecoveryStateEntry(TypedDict):
    consecutive_failures: int
    blocked_until_at: datetime | None
    last_cooldown_seconds: float
    last_failure_kind: FailureKind | None
    probe_eligible_logged: bool


_recovery_state: dict[tuple[int, int], RecoveryStateEntry] = {}


def get_loadbalancer_settings():
    return get_settings()


def current_state_to_recovery_entry(
    current_state: LoadbalanceCurrentState,
) -> RecoveryStateEntry:
    return {
        "consecutive_failures": current_state.consecutive_failures,
        "blocked_until_at": current_state.blocked_until_at,
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


async def clear_current_state(profile_id: int, connection_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            delete(LoadbalanceCurrentState).where(
                LoadbalanceCurrentState.profile_id == profile_id,
                LoadbalanceCurrentState.connection_id == connection_id,
            )
        )
        await session.commit()
        return bool(getattr(result, "rowcount", 0))


async def clear_current_state_for_connection_ids(
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


async def clear_current_state_for_model(profile_id: int, model_config_id: int) -> int:
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
        if not connection_ids:
            await session.rollback()
            return 0

        result = await session.execute(
            delete(LoadbalanceCurrentState).where(
                LoadbalanceCurrentState.profile_id == profile_id,
                LoadbalanceCurrentState.connection_id.in_(connection_ids),
            )
        )
        await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)


async def clear_current_state_for_profile(profile_id: int) -> int:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            delete(LoadbalanceCurrentState).where(
                LoadbalanceCurrentState.profile_id == profile_id,
            )
        )
        await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)


async def mark_probe_eligible_logged(
    *,
    profile_id: int,
    connection_id: int,
    now_at: datetime | None = None,
) -> RecoveryStateEntry | None:
    normalized_now = ensure_utc_datetime(now_at) or utc_now()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LoadbalanceCurrentState)
            .where(
                LoadbalanceCurrentState.profile_id == profile_id,
                LoadbalanceCurrentState.connection_id == connection_id,
            )
            .with_for_update()
        )
        current_state = result.scalar_one_or_none()
        blocked_until_at = ensure_utc_datetime(
            current_state.blocked_until_at if current_state is not None else None
        )
        if (
            current_state is None
            or current_state.probe_eligible_logged
            or blocked_until_at is None
            or blocked_until_at > normalized_now
        ):
            await session.rollback()
            return None

        current_state.probe_eligible_logged = True
        await session.commit()
        return current_state_to_recovery_entry(current_state)


__all__ = [
    "FailureKind",
    "LOGGER_NAME",
    "RecoveryStateEntry",
    "_recovery_state",
    "clear_current_state",
    "clear_current_state_for_connection_ids",
    "clear_current_state_for_model",
    "clear_current_state_for_profile",
    "current_state_to_recovery_entry",
    "get_current_states_for_connections",
    "get_loadbalancer_settings",
    "list_current_states_for_model",
    "logger",
    "mark_probe_eligible_logged",
]
