from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import RequestLog


def _build_request_log_where(
    *,
    profile_id: int,
    request_id: int | None = None,
    model_id: str | None = None,
    provider_type: str | None = None,
    status_code: int | None = None,
    success: bool | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
):
    filters = [RequestLog.profile_id == profile_id]
    if request_id is not None:
        filters.append(RequestLog.id == request_id)
    if model_id:
        filters.append(RequestLog.model_id == model_id)
    if provider_type:
        filters.append(RequestLog.provider_type == provider_type)
    if status_code is not None:
        filters.append(RequestLog.status_code == status_code)
    if success is True:
        filters.append(RequestLog.status_code.between(200, 299))
    elif success is False:
        filters.append(~RequestLog.status_code.between(200, 299))
    if from_time:
        filters.append(RequestLog.created_at >= from_time)
    if to_time:
        filters.append(RequestLog.created_at <= to_time)
    if endpoint_id is not None:
        filters.append(RequestLog.endpoint_id == endpoint_id)
    if connection_id is not None:
        filters.append(RequestLog.connection_id == connection_id)

    return and_(*filters) if filters else literal(True)


async def _get_request_log_total(db: AsyncSession, where) -> int:
    count_q = select(func.count()).select_from(RequestLog).where(where)
    return (await db.execute(count_q)).scalar() or 0


def _request_log_order_by():
    return RequestLog.created_at.desc(), RequestLog.id.desc()


async def get_request_logs(
    db: AsyncSession,
    *,
    profile_id: int,
    request_id: int | None = None,
    model_id: str | None = None,
    provider_type: str | None = None,
    status_code: int | None = None,
    success: bool | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[RequestLog], int]:
    where = _build_request_log_where(
        profile_id=profile_id,
        request_id=request_id,
        model_id=model_id,
        provider_type=provider_type,
        status_code=status_code,
        success=success,
        from_time=from_time,
        to_time=to_time,
        endpoint_id=endpoint_id,
        connection_id=connection_id,
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


async def get_operations_request_logs(
    db: AsyncSession,
    *,
    profile_id: int,
    request_id: int | None = None,
    model_id: str | None = None,
    provider_type: str | None = None,
    status_code: int | None = None,
    success: bool | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    where = _build_request_log_where(
        profile_id=profile_id,
        request_id=request_id,
        model_id=model_id,
        provider_type=provider_type,
        status_code=status_code,
        success=success,
        from_time=from_time,
        to_time=to_time,
        endpoint_id=endpoint_id,
        connection_id=connection_id,
    )
    total = await _get_request_log_total(db, where)

    q = (
        select(
            RequestLog.id.label("id"),
            RequestLog.model_id.label("model_id"),
            RequestLog.provider_type.label("provider_type"),
            RequestLog.status_code.label("status_code"),
            RequestLog.response_time_ms.label("response_time_ms"),
            RequestLog.input_tokens.label("input_tokens"),
            RequestLog.output_tokens.label("output_tokens"),
            RequestLog.total_tokens.label("total_tokens"),
            RequestLog.cache_read_input_tokens.label("cache_read_input_tokens"),
            RequestLog.cache_creation_input_tokens.label("cache_creation_input_tokens"),
            RequestLog.reasoning_tokens.label("reasoning_tokens"),
            RequestLog.total_cost_user_currency_micros.label(
                "total_cost_user_currency_micros"
            ),
            RequestLog.error_detail.label("error_detail"),
            RequestLog.created_at.label("created_at"),
        )
        .where(where)
        .order_by(*_request_log_order_by())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(q)).mappings().all()
    return [dict(row) for row in rows], total
