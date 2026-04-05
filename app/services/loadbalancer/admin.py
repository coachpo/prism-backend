from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import ensure_utc_datetime, utc_now
from app.models.models import Connection, LoadbalanceEvent, ModelConfig
from app.schemas.schemas import (
    LoadbalanceCurrentStateItem,
    LoadbalanceCurrentStateListResponse,
    LoadbalanceCurrentStateResetResponse,
    LoadbalanceCurrentStateValue,
    LoadbalanceEventDetail,
    LoadbalanceEventListItem,
    LoadbalanceEventListResponse,
)

from .state import (
    clear_connection_state,
    clear_round_robin_state_for_model,
    list_current_states_for_model,
)


def _is_banned_now(
    *, ban_mode: str, banned_until_at: datetime | None, now_at: datetime
) -> bool:
    if ban_mode == "manual":
        return True
    normalized_banned_until = ensure_utc_datetime(banned_until_at)
    return normalized_banned_until is not None and normalized_banned_until > now_at


def _derive_current_state_value(
    *,
    ban_mode: str,
    banned_until_at: datetime | None,
    blocked_until_at: datetime | None,
    now_at: datetime,
) -> LoadbalanceCurrentStateValue:
    if _is_banned_now(
        ban_mode=ban_mode,
        banned_until_at=banned_until_at,
        now_at=now_at,
    ):
        return "banned"
    normalized_blocked_until = ensure_utc_datetime(blocked_until_at)
    if normalized_blocked_until is None:
        return "counting"
    if normalized_blocked_until > now_at:
        return "blocked"
    return "probe_eligible"


async def list_model_current_state(
    *,
    db: AsyncSession,
    profile_id: int,
    model_config_id: int,
) -> LoadbalanceCurrentStateListResponse:
    model_exists = await db.scalar(
        select(ModelConfig.id).where(
            ModelConfig.id == model_config_id,
            ModelConfig.profile_id == profile_id,
        )
    )
    if model_exists is None:
        raise HTTPException(status_code=404, detail="Model not found")

    now_at = utc_now()
    rows = await list_current_states_for_model(
        db,
        profile_id=profile_id,
        model_config_id=model_config_id,
    )
    return LoadbalanceCurrentStateListResponse(
        items=[
            LoadbalanceCurrentStateItem(
                connection_id=row.connection_id,
                circuit_state=row.circuit_state,
                probe_available_at=row.probe_available_at,
                window_started_at=row.window_started_at,
                window_request_count=row.window_request_count,
                in_flight_non_stream=row.in_flight_non_stream,
                in_flight_stream=row.in_flight_stream,
                consecutive_failures=row.consecutive_failures,
                last_failure_kind=row.last_failure_kind,
                last_cooldown_seconds=float(row.last_cooldown_seconds),
                max_cooldown_strikes=row.max_cooldown_strikes,
                ban_mode=row.ban_mode,
                banned_until_at=row.banned_until_at,
                blocked_until_at=row.blocked_until_at,
                probe_eligible_logged=row.probe_eligible_logged,
                live_p95_latency_ms=row.live_p95_latency_ms,
                last_live_failure_at=row.last_live_failure_at,
                last_live_success_at=row.last_live_success_at,
                state=_derive_current_state_value(
                    ban_mode=row.ban_mode,
                    banned_until_at=row.banned_until_at,
                    blocked_until_at=row.blocked_until_at,
                    now_at=now_at,
                ),
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ]
    )


async def reset_connection_current_state(
    *,
    db: AsyncSession,
    profile_id: int,
    connection_id: int,
) -> LoadbalanceCurrentStateResetResponse:
    model_config_id = await db.scalar(
        select(Connection.model_config_id).where(
            Connection.profile_id == profile_id,
            Connection.id == connection_id,
        )
    )
    cleared = await clear_connection_state(profile_id, connection_id)
    if model_config_id is not None:
        _ = await clear_round_robin_state_for_model(
            db,
            profile_id=profile_id,
            model_config_id=model_config_id,
        )
    return LoadbalanceCurrentStateResetResponse(
        connection_id=connection_id,
        cleared=cleared,
    )


async def list_model_events(
    *,
    db: AsyncSession,
    profile_id: int,
    model_id: str,
    limit: int,
    offset: int,
) -> LoadbalanceEventListResponse:
    count_q = (
        select(func.count())
        .select_from(LoadbalanceEvent)
        .where(
            LoadbalanceEvent.profile_id == profile_id,
            LoadbalanceEvent.model_id == model_id,
        )
    )
    total = (await db.execute(count_q)).scalar() or 0

    rows = (
        (
            await db.execute(
                select(LoadbalanceEvent)
                .where(
                    LoadbalanceEvent.profile_id == profile_id,
                    LoadbalanceEvent.model_id == model_id,
                )
                .order_by(LoadbalanceEvent.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    return LoadbalanceEventListResponse(
        items=[LoadbalanceEventListItem.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_event_detail(
    *,
    db: AsyncSession,
    profile_id: int,
    event_id: int,
) -> LoadbalanceEventDetail:
    row = (
        await db.execute(
            select(LoadbalanceEvent).where(
                LoadbalanceEvent.id == event_id,
                LoadbalanceEvent.profile_id == profile_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Loadbalance event not found")
    return LoadbalanceEventDetail.model_validate(row)


__all__ = [
    "get_event_detail",
    "list_model_current_state",
    "list_model_events",
    "reset_connection_current_state",
]
