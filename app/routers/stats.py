from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

import app.routers.stats_domains as _stats_impl
from app.dependencies import get_db, get_effective_profile_id
from app.schemas.schemas import (
    BatchDeleteResponse,
    ConnectionMetricsBatchRequest,
    ConnectionMetricsBatchResponse,
    ConnectionSuccessRateResponse,
    ModelMetricsBatchRequest,
    ModelMetricsBatchResponse,
    OperationsRequestLogListResponse,
    RequestLogListResponse,
    SpendingReportResponse,
    StatsSummaryResponse,
    ThroughputStatsResponse,
)
from app.services.background_cleanup import delete_request_logs_in_background
from app.services.stats_service import (
    get_connection_metrics_batch,
    get_connection_success_rates,
    get_model_metrics_batch,
    get_operations_request_logs,
    get_request_logs,
    get_spending_report,
    get_stats_summary,
    get_throughput_stats,
)

router = APIRouter(prefix="/api/stats", tags=["statistics"])


@router.get("/requests", response_model=RequestLogListResponse)
async def list_request_logs(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
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
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    return await _stats_impl.list_request_logs(
        db,
        profile_id,
        request_id=request_id,
        ingress_request_id=ingress_request_id,
        model_id=model_id,
        provider_type=provider_type,
        status_code=status_code,
        status_family=status_family,
        success=success,
        from_time=from_time,
        to_time=to_time,
        endpoint_id=endpoint_id,
        connection_id=connection_id,
        limit=limit,
        offset=offset,
        get_request_logs_fn=get_request_logs,
    )


@router.get("/requests/operations", response_model=OperationsRequestLogListResponse)
async def list_operations_request_logs(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
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
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    return await _stats_impl.list_operations_request_logs(
        db,
        profile_id,
        request_id=request_id,
        ingress_request_id=ingress_request_id,
        model_id=model_id,
        provider_type=provider_type,
        status_code=status_code,
        status_family=status_family,
        success=success,
        from_time=from_time,
        to_time=to_time,
        endpoint_id=endpoint_id,
        connection_id=connection_id,
        limit=limit,
        offset=offset,
        get_operations_request_logs_fn=get_operations_request_logs,
    )


@router.get("/summary", response_model=StatsSummaryResponse)
async def stats_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    group_by: str | None = None,
    model_id: str | None = None,
    provider_type: str | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
):
    return await _stats_impl.stats_summary(
        db,
        profile_id,
        from_time=from_time,
        to_time=to_time,
        group_by=group_by,
        model_id=model_id,
        provider_type=provider_type,
        endpoint_id=endpoint_id,
        connection_id=connection_id,
        get_stats_summary_fn=get_stats_summary,
    )


@router.post("/models/metrics", response_model=ModelMetricsBatchResponse)
async def model_metrics_batch(
    body: ModelMetricsBatchRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await _stats_impl.model_metrics_batch(
        body,
        db,
        profile_id,
        get_model_metrics_batch_fn=get_model_metrics_batch,
    )


@router.post(
    "/models/connections/metrics", response_model=ConnectionMetricsBatchResponse
)
async def connection_metrics_batch(
    body: ConnectionMetricsBatchRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await _stats_impl.connection_metrics_batch(
        body,
        db,
        profile_id,
        get_connection_metrics_batch_fn=get_connection_metrics_batch,
    )


@router.get(
    "/connection-success-rates", response_model=list[ConnectionSuccessRateResponse]
)
async def connection_success_rates(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    from_time: datetime | None = None,
    to_time: datetime | None = None,
):
    return await _stats_impl.connection_success_rates(
        db,
        profile_id,
        from_time=from_time,
        to_time=to_time,
        get_connection_success_rates_fn=get_connection_success_rates,
    )


@router.get("/spending", response_model=SpendingReportResponse)
async def spending_report(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    preset: str | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    provider_type: str | None = None,
    model_id: str | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
    group_by: str = Query(
        default="none",
        pattern="^(none|day|week|month|provider|model|endpoint|model_endpoint)$",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    top_n: int = Query(default=5, ge=1, le=50),
):
    return await _stats_impl.spending_report(
        db,
        profile_id,
        preset=preset,
        from_time=from_time,
        to_time=to_time,
        provider_type=provider_type,
        model_id=model_id,
        endpoint_id=endpoint_id,
        connection_id=connection_id,
        group_by=group_by,
        limit=limit,
        offset=offset,
        top_n=top_n,
        get_spending_report_fn=get_spending_report,
    )


@router.delete("/requests", response_model=BatchDeleteResponse)
async def delete_request_logs(
    background_tasks: BackgroundTasks,
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    older_than_days: int | None = Query(default=None, ge=1),
    delete_all: bool = Query(default=False),
):
    return await _stats_impl.delete_request_logs(
        background_tasks,
        profile_id,
        older_than_days=older_than_days,
        delete_all=delete_all,
        delete_request_logs_in_background_fn=delete_request_logs_in_background,
    )


@router.get("/throughput", response_model=ThroughputStatsResponse)
async def get_throughput(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    model_id: str | None = None,
    provider_type: str | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
):
    return await _stats_impl.get_throughput(
        db,
        profile_id,
        from_time=from_time,
        to_time=to_time,
        model_id=model_id,
        provider_type=provider_type,
        endpoint_id=endpoint_id,
        connection_id=connection_id,
        get_throughput_stats_fn=get_throughput_stats,
    )


__all__ = [
    "connection_metrics_batch",
    "connection_success_rates",
    "delete_request_logs",
    "get_throughput",
    "list_operations_request_logs",
    "list_request_logs",
    "model_metrics_batch",
    "router",
    "spending_report",
    "stats_summary",
]
