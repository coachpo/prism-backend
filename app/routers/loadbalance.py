from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import ensure_utc_datetime, utc_now
from app.dependencies import get_db, get_effective_profile_id
from app.models.models import LoadbalanceEvent, ModelConfig
from app.schemas.schemas import (
    LoadbalanceCurrentStateValue,
    LoadbalanceCurrentStateItem,
    LoadbalanceCurrentStateListResponse,
    LoadbalanceCurrentStateResetResponse,
    LoadbalanceEventDeleteResponse,
    LoadbalanceEventDetail,
    LoadbalanceEventListItem,
    LoadbalanceEventListResponse,
)
from app.services.loadbalance_cleanup import delete_loadbalance_events_in_background
from app.services.loadbalancer import clear_current_state, list_current_states_for_model

router = APIRouter(prefix="/api/loadbalance", tags=["loadbalance"])


def _derive_current_state_value(
    *,
    blocked_until_at: datetime | None,
    now_at: datetime,
) -> LoadbalanceCurrentStateValue:
    normalized_blocked_until = ensure_utc_datetime(blocked_until_at)
    if normalized_blocked_until is None:
        return "counting"
    if normalized_blocked_until > now_at:
        return "blocked"
    return "probe_eligible"


@router.get("/current-state", response_model=LoadbalanceCurrentStateListResponse)
async def list_loadbalance_current_state(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    model_config_id: int = Query(ge=1),
):
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
                consecutive_failures=row.consecutive_failures,
                last_failure_kind=row.last_failure_kind,
                last_cooldown_seconds=float(row.last_cooldown_seconds),
                blocked_until_at=row.blocked_until_at,
                probe_eligible_logged=row.probe_eligible_logged,
                state=_derive_current_state_value(
                    blocked_until_at=row.blocked_until_at,
                    now_at=now_at,
                ),
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ]
    )


@router.post(
    "/current-state/{connection_id}/reset",
    response_model=LoadbalanceCurrentStateResetResponse,
)
async def reset_loadbalance_current_state(
    connection_id: int,
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    cleared = await clear_current_state(profile_id, connection_id)
    return LoadbalanceCurrentStateResetResponse(
        connection_id=connection_id,
        cleared=cleared,
    )


@router.get("/events", response_model=LoadbalanceEventListResponse)
async def list_loadbalance_events(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    model_id: str = Query(min_length=1),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    count_q = (
        select(func.count())
        .select_from(LoadbalanceEvent)
        .where(
            LoadbalanceEvent.profile_id == profile_id,
            LoadbalanceEvent.model_id == model_id,
        )
    )
    total = (await db.execute(count_q)).scalar() or 0

    q = (
        select(LoadbalanceEvent)
        .where(
            LoadbalanceEvent.profile_id == profile_id,
            LoadbalanceEvent.model_id == model_id,
        )
        .order_by(LoadbalanceEvent.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(q)).scalars().all()

    return LoadbalanceEventListResponse(
        items=[LoadbalanceEventListItem.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/events/{event_id}", response_model=LoadbalanceEventDetail)
async def get_loadbalance_event(
    event_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    result = await db.execute(
        select(LoadbalanceEvent).where(
            LoadbalanceEvent.id == event_id,
            LoadbalanceEvent.profile_id == profile_id,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Loadbalance event not found")
    return LoadbalanceEventDetail.model_validate(row)


@router.delete("/events", response_model=LoadbalanceEventDeleteResponse)
async def delete_loadbalance_events(
    background_tasks: BackgroundTasks,
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    before: datetime | None = None,
    older_than_days: int | None = Query(default=None, ge=1),
    delete_all: bool = Query(default=False),
):
    normalized_before = ensure_utc_datetime(before)

    provided = sum([before is not None, older_than_days is not None, delete_all])
    if provided != 1:
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of 'before', 'older_than_days', or 'delete_all'",
        )

    if before is not None and normalized_before is None:
        raise HTTPException(status_code=400, detail="'before' is required")

    background_tasks.add_task(
        delete_loadbalance_events_in_background,
        profile_id=profile_id,
        before=normalized_before,
        older_than_days=older_than_days,
        delete_all=delete_all,
    )
    return LoadbalanceEventDeleteResponse(accepted=True)
