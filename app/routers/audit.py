from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, delete, and_, literal
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.models.models import AuditLog
from app.schemas.schemas import (
    AuditLogListItem,
    AuditLogDetail,
    AuditLogListResponse,
    AuditLogDeleteResponse,
)

router = APIRouter(prefix="/api/audit", tags=["audit"])

VALID_OLDER_THAN_DAYS = {7, 15, 30}


@router.get("/logs", response_model=AuditLogListResponse)
async def list_audit_logs(
    db: Annotated[AsyncSession, Depends(get_db)],
    provider_id: int | None = None,
    model_id: str | None = None,
    status_code: int | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    filters = []
    if provider_id is not None:
        filters.append(AuditLog.provider_id == provider_id)
    if model_id:
        filters.append(AuditLog.model_id == model_id)
    if status_code is not None:
        filters.append(AuditLog.response_status == status_code)
    if from_time:
        filters.append(AuditLog.created_at >= from_time)
    if to_time:
        filters.append(AuditLog.created_at <= to_time)

    where = and_(*filters) if filters else literal(True)

    count_q = select(func.count()).select_from(AuditLog).where(where)
    total = (await db.execute(count_q)).scalar() or 0

    q = (
        select(AuditLog)
        .where(where)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(q)).scalars().all()

    items = []
    for row in rows:
        preview = None
        if row.request_body:
            preview = row.request_body[:200]
        items.append(
            AuditLogListItem(
                id=row.id,
                request_log_id=row.request_log_id,
                provider_id=row.provider_id,
                model_id=row.model_id,
                request_method=row.request_method,
                request_url=row.request_url,
                request_headers=row.request_headers,
                request_body_preview=preview,
                response_status=row.response_status,
                is_stream=row.is_stream,
                duration_ms=row.duration_ms,
                created_at=row.created_at,
            )
        )

    return AuditLogListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/logs/{log_id}", response_model=AuditLogDetail)
async def get_audit_log(
    log_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(AuditLog).where(AuditLog.id == log_id))
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Audit log not found")
    return row


@router.delete("/logs", response_model=AuditLogDeleteResponse)
async def delete_audit_logs(
    db: Annotated[AsyncSession, Depends(get_db)],
    before: datetime | None = None,
    older_than_days: int | None = None,
):
    if before and older_than_days is not None:
        raise HTTPException(
            status_code=400,
            detail="Provide either 'before' or 'older_than_days', not both",
        )
    if before is None and older_than_days is None:
        raise HTTPException(
            status_code=400,
            detail="Provide either 'before' or 'older_than_days'",
        )

    if older_than_days is not None:
        if older_than_days not in VALID_OLDER_THAN_DAYS:
            raise HTTPException(
                status_code=400,
                detail=f"older_than_days must be one of: {sorted(VALID_OLDER_THAN_DAYS)}",
            )
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            days=older_than_days
        )
    else:
        cutoff = before

    stmt = delete(AuditLog).where(AuditLog.created_at < cutoff)
    result = await db.execute(stmt)
    await db.flush()
    return AuditLogDeleteResponse(deleted_count=result.rowcount)
