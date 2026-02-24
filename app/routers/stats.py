from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.models.models import RequestLog
from app.schemas.schemas import (
    RequestLogListResponse,
    RequestLogResponse,
    StatsSummaryResponse,
    EndpointSuccessRateResponse,
    BatchDeleteResponse,
    SpendingReportResponse,
)
from app.services.stats_service import (
    get_request_logs,
    get_stats_summary,
    get_endpoint_success_rates,
    get_spending_report,
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
    serialized_items: list[RequestLogResponse] = [
        RequestLogResponse.model_validate(item) for item in items
    ]
    return RequestLogListResponse(
        items=serialized_items,
        total=total,
        limit=limit,
        offset=offset,
    )


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


@router.get("/spending", response_model=SpendingReportResponse)
async def spending_report(
    db: Annotated[AsyncSession, Depends(get_db)],
    preset: str | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    provider_type: str | None = None,
    model_id: str | None = None,
    endpoint_id: int | None = None,
    group_by: str = Query(
        default="none",
        pattern="^(none|day|week|month|provider|model|endpoint|model_endpoint)$",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    top_n: int = Query(default=5, ge=1, le=50),
):
    return await get_spending_report(
        db,
        preset=preset,
        from_time=from_time,
        to_time=to_time,
        provider_type=provider_type,
        model_id=model_id,
        endpoint_id=endpoint_id,
        group_by=group_by,
        limit=limit,
        offset=offset,
        top_n=top_n,
    )


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
        if older_than_days is None:
            raise HTTPException(
                status_code=400,
                detail="older_than_days is required when delete_all is false",
            )
        days = int(older_than_days)
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        stmt = delete(RequestLog).where(RequestLog.created_at < cutoff)

    result = await db.execute(stmt)
    await db.flush()
    rowcount = getattr(result, "rowcount", 0)
    return BatchDeleteResponse(deleted_count=int(rowcount or 0))
