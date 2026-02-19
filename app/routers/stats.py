from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.schemas import RequestLogListResponse, StatsSummaryResponse
from app.services.stats_service import get_request_logs, get_stats_summary

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
):
    result = await get_stats_summary(
        db,
        from_time=from_time,
        to_time=to_time,
        group_by=group_by,
    )
    return StatsSummaryResponse(**result)
