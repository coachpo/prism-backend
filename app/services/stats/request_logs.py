from datetime import datetime
from typing import Literal

from sqlalchemy import and_, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import RequestLog


def _build_request_log_browse_where(
    *,
    profile_id: int,
    ingress_request_id: str | None = None,
    model_id: str | None = None,
    status_family: Literal["4xx", "5xx"] | None = None,
    from_time: datetime | None = None,
    endpoint_id: int | None = None,
):
    filters = [RequestLog.profile_id == profile_id]
    if ingress_request_id:
        filters.append(RequestLog.ingress_request_id == ingress_request_id)
    if model_id:
        filters.append(RequestLog.model_id == model_id)
    if status_family == "4xx":
        filters.append(RequestLog.status_code.between(400, 499))
    elif status_family == "5xx":
        filters.append(RequestLog.status_code.between(500, 599))
    if from_time:
        filters.append(RequestLog.created_at >= from_time)
    if endpoint_id is not None:
        filters.append(RequestLog.endpoint_id == endpoint_id)

    return and_(*filters) if filters else literal(True)


def _build_request_log_detail_where(*, profile_id: int, request_id: int):
    return and_(
        RequestLog.profile_id == profile_id,
        RequestLog.id == request_id,
    )


async def _get_request_log_total(db: AsyncSession, where) -> int:
    count_q = select(func.count()).select_from(RequestLog).where(where)
    return (await db.execute(count_q)).scalar() or 0


def _request_log_order_by():
    return RequestLog.created_at.desc(), RequestLog.id.desc()


async def get_request_logs(
    db: AsyncSession,
    *,
    profile_id: int,
    ingress_request_id: str | None = None,
    model_id: str | None = None,
    status_family: Literal["4xx", "5xx"] | None = None,
    from_time: datetime | None = None,
    endpoint_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[RequestLog], int]:
    where = _build_request_log_browse_where(
        profile_id=profile_id,
        ingress_request_id=ingress_request_id,
        model_id=model_id,
        status_family=status_family,
        from_time=from_time,
        endpoint_id=endpoint_id,
    )
    total = await _get_request_log_total(db, where)

    q = (
        select(RequestLog)
        .where(where)
        .order_by(*_request_log_order_by())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(q)).scalars().all()
    return list(rows), total


async def get_request_log_detail(
    db: AsyncSession,
    *,
    profile_id: int,
    request_id: int,
) -> RequestLog | None:
    where = _build_request_log_detail_where(
        profile_id=profile_id,
        request_id=request_id,
    )
    q = select(RequestLog).where(where).limit(1)
    return (await db.execute(q)).scalar_one_or_none()
