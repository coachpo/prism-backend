from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

import app.routers.stats_domains as _stats_impl
from app.routers.stats_domains import request_logs_route_handlers as _request_log_impl
from app.dependencies import get_db, get_effective_profile_id
from app.schemas.schemas import (
    BatchDeleteResponse,
    ConnectionSuccessRateResponse,
    ModelMetricsBatchRequest,
    ModelMetricsBatchResponse,
    RequestLogDetailResponse,
    RequestLogListResponse,
    SpendingReportResponse,
    StatsSummaryResponse,
    ThroughputStatsResponse,
    UsageModelStatistic,
    UsageSnapshotResponse,
)
from app.services.background_cleanup import (
    delete_request_logs_in_background,
    delete_statistics_in_background,
)
from app.services.stats.request_logs import get_request_log_detail
from app.services.stats_service import (
    get_connection_success_rates,
    get_endpoint_model_statistics,
    get_model_metrics_batch,
    get_request_logs,
    get_spending_report,
    get_stats_summary,
    get_throughput_stats,
    get_usage_snapshot,
)

router = APIRouter(prefix="/api/stats", tags=["statistics"])


@router.get("/requests", response_model=RequestLogListResponse)
async def list_request_logs(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    ingress_request_id: str | None = None,
    model_id: str | None = None,
    status_family: Literal["4xx", "5xx"] | None = None,
    from_time: datetime | None = None,
    endpoint_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    return await _stats_impl.list_request_logs(
        db,
        profile_id,
        ingress_request_id=ingress_request_id,
        model_id=model_id,
        status_family=status_family,
        from_time=from_time,
        endpoint_id=endpoint_id,
        limit=limit,
        offset=offset,
        get_request_logs_fn=get_request_logs,
    )


@router.get("/requests/{request_id}", response_model=RequestLogDetailResponse)
async def request_log_detail(
    request_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await _request_log_impl.request_log_detail(
        db,
        profile_id,
        request_id=request_id,
        get_request_log_detail_fn=get_request_log_detail,
    )


@router.get("/summary", response_model=StatsSummaryResponse)
async def stats_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    group_by: str | None = None,
    model_id: str | None = None,
    api_family: str | None = None,
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
        api_family=api_family,
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
    api_family: str | None = None,
    model_id: str | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
    group_by: str = Query(
        default="none",
        pattern="^(none|day|week|month|api_family|model|endpoint|model_endpoint)$",
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
        api_family=api_family,
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


@router.delete("/statistics", response_model=BatchDeleteResponse)
async def delete_statistics_data(
    background_tasks: BackgroundTasks,
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    older_than_days: int | None = Query(default=None, ge=1),
    delete_all: bool = Query(default=False),
):
    return await _stats_impl.delete_statistics_data(
        background_tasks,
        profile_id,
        older_than_days=older_than_days,
        delete_all=delete_all,
        delete_statistics_in_background_fn=delete_statistics_in_background,
    )


@router.get("/throughput", response_model=ThroughputStatsResponse)
async def get_throughput(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    model_id: str | None = None,
    api_family: str | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
):
    return await _stats_impl.get_throughput(
        db,
        profile_id,
        from_time=from_time,
        to_time=to_time,
        model_id=model_id,
        api_family=api_family,
        endpoint_id=endpoint_id,
        connection_id=connection_id,
        get_throughput_stats_fn=get_throughput_stats,
    )


@router.get("/usage-snapshot", response_model=UsageSnapshotResponse)
async def usage_snapshot(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    preset: Literal["all", "7h", "24h", "7d"] = "24h",
):
    return await _stats_impl.usage_snapshot(
        db,
        profile_id,
        preset=preset,
        get_usage_snapshot_fn=get_usage_snapshot,
    )


@router.get("/endpoints/{endpoint_id}/models", response_model=list[UsageModelStatistic])
async def endpoint_model_statistics(
    endpoint_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    preset: Literal["all", "7h", "24h", "7d"] | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
):
    return await _stats_impl.endpoint_model_statistics(
        db,
        profile_id,
        endpoint_id=endpoint_id,
        preset=preset,
        from_time=from_time,
        to_time=to_time,
        get_endpoint_model_statistics_fn=get_endpoint_model_statistics,
    )


__all__ = [
    "connection_success_rates",
    "delete_request_logs",
    "endpoint_model_statistics",
    "get_throughput",
    "list_request_logs",
    "model_metrics_batch",
    "request_log_detail",
    "router",
    "spending_report",
    "stats_summary",
    "usage_snapshot",
]
