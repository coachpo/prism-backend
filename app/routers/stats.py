from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.models.models import RequestLog
from app.schemas.schemas import (
    RequestLogListResponse,
    StatsSummaryResponse,
    EndpointSuccessRateResponse,
    BatchDeleteResponse,
)
from app.services.stats_service import (
    get_request_logs,
    get_stats_summary,
    get_endpoint_success_rates,
)

router = APIRouter(prefix="/api/stats", tags=["statistics"])


@router.get("/requests", response_model=RequestLogListResponse)
async def list_request_logs(
    db: Annotated[AsyncSession, Depends(get_db)],
    model_id: str | None = None,
    provider_type: str | None = None,
    status_code: int | None = None,
    success: bool | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    endpoint_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    items, total = await get_request_logs(
        db,
        model_id=model_id,
        provider_type=provider_type,
        status_code=status_code,
        success=success,
        from_time=from_time,
        to_time=to_time,
        endpoint_id=endpoint_id,
        limit=limit,
        offset=offset,
    )
    return RequestLogListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/summary", response_model=StatsSummaryResponse)
async def stats_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    group_by: str | None = None,
    model_id: str | None = None,
    provider_type: str | None = None,
    endpoint_id: int | None = None,
):
    result = await get_stats_summary(
        db,
        from_time=from_time,
        to_time=to_time,
        group_by=group_by,
        model_id=model_id,
        provider_type=provider_type,
        endpoint_id=endpoint_id,
    )
    return StatsSummaryResponse(**result)


@router.get("/endpoint-success-rates", response_model=list[EndpointSuccessRateResponse])
async def endpoint_success_rates(
    db: Annotated[AsyncSession, Depends(get_db)],
    from_time: datetime | None = None,
    to_time: datetime | None = None,
):
    return await get_endpoint_success_rates(db, from_time=from_time, to_time=to_time)


@router.delete("/requests", response_model=BatchDeleteResponse)
async def delete_request_logs(
    db: Annotated[AsyncSession, Depends(get_db)],
    older_than_days: int | None = Query(default=None, ge=1),
    delete_all: bool = Query(default=False),
):
    if delete_all and older_than_days is not None:
        raise HTTPException(
            status_code=400,
            detail="Provide either 'older_than_days' or 'delete_all', not both",
        )
    if not delete_all and older_than_days is None:
        raise HTTPException(
            status_code=400,
            detail="Provide either 'older_than_days' (integer >= 1) or 'delete_all=true'",
        )

    if delete_all:
        stmt = delete(RequestLog)
    else:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            days=older_than_days  # type: ignore[arg-type]
        )
        stmt = delete(RequestLog).where(RequestLog.created_at < cutoff)

    result = await db.execute(stmt)
    await db.flush()
    return BatchDeleteResponse(deleted_count=result.rowcount)
