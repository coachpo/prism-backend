from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, delete, and_, literal
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import ensure_utc_datetime, utc_now
from app.dependencies import get_db, get_effective_profile_id
from app.models.models import AuditLog
from app.schemas.schemas import (
    AuditLogListItem,
    AuditLogDetail,
    AuditLogListResponse,
    AuditLogDeleteResponse,
)

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/logs", response_model=AuditLogListResponse)
async def list_audit_logs(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    provider_id: int | None = None,
    model_id: str | None = None,
    status_code: int | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    normalized_from_time = ensure_utc_datetime(from_time)
    normalized_to_time = ensure_utc_datetime(to_time)

    filters = [AuditLog.profile_id == profile_id]
    if provider_id is not None:
        filters.append(AuditLog.provider_id == provider_id)
    if model_id:
        filters.append(AuditLog.model_id == model_id)
    if status_code is not None:
        filters.append(AuditLog.response_status == status_code)
    if endpoint_id is not None:
        filters.append(AuditLog.endpoint_id == endpoint_id)
    if connection_id is not None:
        filters.append(AuditLog.connection_id == connection_id)
    if normalized_from_time:
        filters.append(AuditLog.created_at >= normalized_from_time)
    if normalized_to_time:
        filters.append(AuditLog.created_at <= normalized_to_time)

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
                profile_id=row.profile_id,
                model_id=row.model_id,
                endpoint_id=row.endpoint_id,
                connection_id=row.connection_id,
                endpoint_base_url=row.endpoint_base_url,
                endpoint_description=row.endpoint_description,
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
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    result = await db.execute(select(AuditLog).where(AuditLog.id == log_id, AuditLog.profile_id == profile_id))
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Audit log not found")
    return row


@router.delete("/logs", response_model=AuditLogDeleteResponse)
async def delete_audit_logs(
    db: Annotated[AsyncSession, Depends(get_db)],
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

    if delete_all:
        stmt = delete(AuditLog).where(AuditLog.profile_id == profile_id)
    elif older_than_days is not None:
        cutoff = utc_now() - timedelta(days=older_than_days)
        stmt = delete(AuditLog).where(
            AuditLog.profile_id == profile_id,
            AuditLog.created_at < cutoff,
        )
    else:
        if normalized_before is None:
            raise HTTPException(status_code=400, detail="'before' is required")
        stmt = delete(AuditLog).where(
            AuditLog.profile_id == profile_id,
            AuditLog.created_at < normalized_before,
        )

    result = await db.execute(stmt)
    await db.flush()
    return AuditLogDeleteResponse(deleted_count=result.rowcount)
