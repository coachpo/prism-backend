from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, delete, and_, literal, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import ensure_utc_datetime, utc_now
from app.dependencies import get_db, get_effective_profile_id
from app.models.models import LoadbalanceEvent
from app.schemas.schemas import (
    LoadbalanceEventListItem,
    LoadbalanceEventDetail,
    LoadbalanceEventListResponse,
    LoadbalanceEventDeleteResponse,
    LoadbalanceStatsResponse,
)

router = APIRouter(prefix="/api/loadbalance", tags=["loadbalance"])


@router.get("/events", response_model=LoadbalanceEventListResponse)
async def list_loadbalance_events(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    connection_id: int | None = None,
    event_type: str | None = None,
    failure_kind: str | None = None,
    model_id: str | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List loadbalance events with optional filtering."""
    normalized_from_time = ensure_utc_datetime(from_time)
    normalized_to_time = ensure_utc_datetime(to_time)

    filters = [LoadbalanceEvent.profile_id == profile_id]
    if connection_id is not None:
        filters.append(LoadbalanceEvent.connection_id == connection_id)
    if event_type:
        filters.append(LoadbalanceEvent.event_type == event_type)
    if failure_kind:
        filters.append(LoadbalanceEvent.failure_kind == failure_kind)
    if model_id:
        filters.append(LoadbalanceEvent.model_id == model_id)
    if normalized_from_time:
        filters.append(LoadbalanceEvent.created_at >= normalized_from_time)
    if normalized_to_time:
        filters.append(LoadbalanceEvent.created_at <= normalized_to_time)

    where = and_(*filters) if filters else literal(True)

    count_q = select(func.count()).select_from(LoadbalanceEvent).where(where)
    total = (await db.execute(count_q)).scalar() or 0

    q = (
        select(LoadbalanceEvent)
        .where(where)
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
    """Get detailed information about a specific loadbalance event."""
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
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    before: datetime | None = None,
    older_than_days: int | None = Query(default=None, ge=1),
    delete_all: bool = Query(default=False),
):
    """Delete loadbalance events based on time criteria."""
    normalized_before = ensure_utc_datetime(before)

    provided = sum([before is not None, older_than_days is not None, delete_all])
    if provided != 1:
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of 'before', 'older_than_days', or 'delete_all'",
        )

    if delete_all:
        stmt = delete(LoadbalanceEvent).where(LoadbalanceEvent.profile_id == profile_id)
    elif older_than_days is not None:
        cutoff = utc_now() - timedelta(days=older_than_days)
        stmt = delete(LoadbalanceEvent).where(
            LoadbalanceEvent.profile_id == profile_id,
            LoadbalanceEvent.created_at < cutoff,
        )
    else:
        if normalized_before is None:
            raise HTTPException(status_code=400, detail="'before' is required")
        stmt = delete(LoadbalanceEvent).where(
            LoadbalanceEvent.profile_id == profile_id,
            LoadbalanceEvent.created_at < normalized_before,
        )

    result = await db.execute(stmt)
    await db.flush()
    rowcount = getattr(result, "rowcount", 0)
    return LoadbalanceEventDeleteResponse(deleted_count=int(rowcount or 0))


@router.get("/stats", response_model=LoadbalanceStatsResponse)
async def get_loadbalance_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    from_time: datetime | None = None,
    to_time: datetime | None = None,
):
    """Get aggregate statistics about loadbalance events."""
    normalized_from_time = ensure_utc_datetime(from_time)
    normalized_to_time = ensure_utc_datetime(to_time)

    filters = [LoadbalanceEvent.profile_id == profile_id]
    if normalized_from_time:
        filters.append(LoadbalanceEvent.created_at >= normalized_from_time)
    if normalized_to_time:
        filters.append(LoadbalanceEvent.created_at <= normalized_to_time)

    where = and_(*filters) if filters else literal(True)

    total_q = select(func.count()).select_from(LoadbalanceEvent).where(where)
    total_events = (await db.execute(total_q)).scalar() or 0

    events_by_type_q = (
        select(
            LoadbalanceEvent.event_type,
            func.count().label("count"),
        )
        .where(where)
        .group_by(LoadbalanceEvent.event_type)
    )
    events_by_type_rows = (await db.execute(events_by_type_q)).all()
    events_by_type = {row.event_type: row.count for row in events_by_type_rows}

    most_failed_q = (
        select(
            LoadbalanceEvent.connection_id,
            func.count().label("failure_count"),
        )
        .where(
            and_(
                where,
                LoadbalanceEvent.event_type.in_(["opened", "extended"]),
            )
        )
        .group_by(LoadbalanceEvent.connection_id)
        .order_by(desc("failure_count"))
        .limit(10)
    )
    most_failed_rows = (await db.execute(most_failed_q)).all()
    most_failed_connections = [
        {"connection_id": row.connection_id, "failure_count": row.failure_count}
        for row in most_failed_rows
    ]

    return LoadbalanceStatsResponse(
        total_events=total_events,
        events_by_type=events_by_type,
        most_failed_connections=most_failed_connections,
    )
