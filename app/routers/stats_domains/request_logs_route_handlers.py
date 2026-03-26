from datetime import datetime
from typing import Literal

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.schemas import (
    BatchDeleteResponse,
    OperationsRequestLogListResponse,
    OperationsRequestLogResponse,
    RequestLogListResponse,
    RequestLogResponse,
)
from app.services.background_cleanup import delete_request_logs_in_background
from app.services.stats_service import get_operations_request_logs, get_request_logs

from .helpers import normalize_datetime_filter


async def list_request_logs(
    db: AsyncSession,
    profile_id: int,
    request_id: int | None = None,
    ingress_request_id: str | None = None,
    model_id: str | None = None,
    provider_type: str | None = None,
    status_code: int | None = None,
    status_family: Literal["4xx", "5xx"] | None = None,
    success: bool | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    *,
    get_request_logs_fn=get_request_logs,
):
    normalized_from_time = normalize_datetime_filter(from_time)
    normalized_to_time = normalize_datetime_filter(to_time)

    items, total = await get_request_logs_fn(
        db,
        request_id=request_id,
        ingress_request_id=ingress_request_id,
        model_id=model_id,
        profile_id=profile_id,
        provider_type=provider_type,
        status_code=status_code,
        status_family=status_family,
        success=success,
        from_time=normalized_from_time,
        to_time=normalized_to_time,
        endpoint_id=endpoint_id,
        connection_id=connection_id,
        limit=limit,
        offset=offset,
    )
    serialized_items = [RequestLogResponse.model_validate(item) for item in items]
    return RequestLogListResponse(
        items=serialized_items,
        total=total,
        limit=limit,
        offset=offset,
    )


async def list_operations_request_logs(
    db: AsyncSession,
    profile_id: int,
    request_id: int | None = None,
    ingress_request_id: str | None = None,
    model_id: str | None = None,
    provider_type: str | None = None,
    status_code: int | None = None,
    status_family: Literal["4xx", "5xx"] | None = None,
    success: bool | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
    limit: int = 200,
    offset: int = 0,
    *,
    get_operations_request_logs_fn=get_operations_request_logs,
):
    normalized_from_time = normalize_datetime_filter(from_time)
    normalized_to_time = normalize_datetime_filter(to_time)

    items, total = await get_operations_request_logs_fn(
        db,
        request_id=request_id,
        ingress_request_id=ingress_request_id,
        model_id=model_id,
        profile_id=profile_id,
        provider_type=provider_type,
        status_code=status_code,
        status_family=status_family,
        success=success,
        from_time=normalized_from_time,
        to_time=normalized_to_time,
        endpoint_id=endpoint_id,
        connection_id=connection_id,
        limit=limit,
        offset=offset,
    )
    serialized_items = [
        OperationsRequestLogResponse.model_validate(item) for item in items
    ]
    return OperationsRequestLogListResponse(
        items=serialized_items,
        total=total,
        limit=limit,
        offset=offset,
    )


async def delete_request_logs(
    background_tasks: BackgroundTasks,
    profile_id: int,
    older_than_days: int | None = None,
    delete_all: bool = False,
    *,
    delete_request_logs_in_background_fn=delete_request_logs_in_background,
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

    background_tasks.add_task(
        delete_request_logs_in_background_fn,
        profile_id=profile_id,
        older_than_days=older_than_days,
        delete_all=delete_all,
    )
    return BatchDeleteResponse(accepted=True)


__all__ = [
    "delete_request_logs",
    "list_operations_request_logs",
    "list_request_logs",
]
