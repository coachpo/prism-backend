from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import ensure_utc_datetime, utc_now
from app.dependencies import get_db, get_effective_profile_id
from app.models.models import RequestLog
from app.schemas.schemas import (
    ConnectionMetricsBatchItem,
    ConnectionMetricsBatchRequest,
    ConnectionMetricsBatchResponse,
    RequestLogListResponse,
    RequestLogResponse,
    ModelMetricsBatchItem,
    ModelMetricsBatchRequest,
    ModelMetricsBatchResponse,
    StatsSummaryResponse,
    ConnectionSuccessRateResponse,
    BatchDeleteResponse,
    SpendingReportResponse,
    ThroughputStatsResponse,
)
from app.services.stats_service import (
    get_connection_metrics_batch,
    get_request_logs,
    get_stats_summary,
    get_connection_success_rates,
    get_model_metrics_batch,
    get_spending_report,
    get_throughput_stats,
)

router = APIRouter(prefix="/api/stats", tags=["statistics"])


def _normalize_datetime_filter(value: datetime | None) -> datetime | None:
    return ensure_utc_datetime(value)


@router.get("/requests", response_model=RequestLogListResponse)
async def list_request_logs(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    request_id: int | None = None,
    model_id: str | None = None,
    provider_type: str | None = None,
    status_code: int | None = None,
    success: bool | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    normalized_from_time = _normalize_datetime_filter(from_time)
    normalized_to_time = _normalize_datetime_filter(to_time)

    items, total = await get_request_logs(
        db,
        request_id=request_id,
        model_id=model_id,
        profile_id=profile_id,
        provider_type=provider_type,
        status_code=status_code,
        success=success,
        from_time=normalized_from_time,
        to_time=normalized_to_time,
        endpoint_id=endpoint_id,
        connection_id=connection_id,
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
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    group_by: str | None = None,
    model_id: str | None = None,
    provider_type: str | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
):
    normalized_from_time = _normalize_datetime_filter(from_time)
    normalized_to_time = _normalize_datetime_filter(to_time)

    result = await get_stats_summary(
        db,
        from_time=normalized_from_time,
        profile_id=profile_id,
        to_time=normalized_to_time,
        group_by=group_by,
        model_id=model_id,
        provider_type=provider_type,
        endpoint_id=endpoint_id,
        connection_id=connection_id,
    )
    return StatsSummaryResponse(**result)


@router.post("/models/metrics", response_model=ModelMetricsBatchResponse)
async def model_metrics_batch(
    body: ModelMetricsBatchRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    items = await get_model_metrics_batch(
        db,
        profile_id=profile_id,
        model_ids=body.model_ids,
        summary_window_hours=body.summary_window_hours,
        spending_preset=body.spending_preset,
    )

    def build_model_metrics_item(model_id: str) -> ModelMetricsBatchItem:
        metric_values = items.get(model_id, {})
        return ModelMetricsBatchItem(
            model_id=model_id,
            success_rate=float(metric_values.get("success_rate", 0.0)),
            request_count_24h=int(metric_values.get("request_count_24h", 0)),
            p95_latency_ms=int(metric_values.get("p95_latency_ms", 0)),
            spend_30d_micros=int(metric_values.get("spend_30d_micros", 0)),
        )

    return ModelMetricsBatchResponse(
        items=[build_model_metrics_item(model_id) for model_id in body.model_ids]
    )


@router.post(
    "/models/connections/metrics", response_model=ConnectionMetricsBatchResponse
)
async def connection_metrics_batch(
    body: ConnectionMetricsBatchRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    items = await get_connection_metrics_batch(
        db,
        profile_id=profile_id,
        model_id=body.model_id,
        connection_ids=body.connection_ids,
        summary_window_hours=body.summary_window_hours,
    )

    def build_connection_metrics_item(connection_id: int) -> ConnectionMetricsBatchItem:
        metric_values = items.get(connection_id, {})
        success_rate_24h = metric_values.get("success_rate_24h")
        p95_latency_ms = metric_values.get("p95_latency_ms")
        five_xx_rate = metric_values.get("five_xx_rate")
        last_failover_like_at = metric_values.get("last_failover_like_at")

        return ConnectionMetricsBatchItem(
            connection_id=connection_id,
            success_rate_24h=(
                float(success_rate_24h) if success_rate_24h is not None else None
            ),
            request_count_24h=int(metric_values.get("request_count_24h", 0)),
            p95_latency_ms=(
                int(p95_latency_ms) if p95_latency_ms is not None else None
            ),
            five_xx_rate=float(five_xx_rate) if five_xx_rate is not None else None,
            heuristic_failover_events=int(
                metric_values.get("heuristic_failover_events", 0)
            ),
            last_failover_like_at=(
                last_failover_like_at
                if isinstance(last_failover_like_at, datetime)
                else None
            ),
        )

    return ConnectionMetricsBatchResponse(
        items=[
            build_connection_metrics_item(connection_id)
            for connection_id in body.connection_ids
        ]
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
    normalized_from_time = _normalize_datetime_filter(from_time)
    normalized_to_time = _normalize_datetime_filter(to_time)

    return await get_connection_success_rates(
        db,
        profile_id=profile_id,
        from_time=normalized_from_time,
        to_time=normalized_to_time,
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
    normalized_from_time = _normalize_datetime_filter(from_time)
    normalized_to_time = _normalize_datetime_filter(to_time)

    return await get_spending_report(
        db,
        preset=preset,
        from_time=normalized_from_time,
        profile_id=profile_id,
        to_time=normalized_to_time,
        provider_type=provider_type,
        model_id=model_id,
        endpoint_id=endpoint_id,
        connection_id=connection_id,
        group_by=group_by,
        limit=limit,
        offset=offset,
        top_n=top_n,
    )


@router.delete("/requests", response_model=BatchDeleteResponse)
async def delete_request_logs(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
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
        stmt = delete(RequestLog).where(RequestLog.profile_id == profile_id)
    else:
        if older_than_days is None:
            raise HTTPException(
                status_code=400,
                detail="older_than_days is required when delete_all is false",
            )
        days = int(older_than_days)
        cutoff = utc_now() - timedelta(days=days)
        stmt = delete(RequestLog).where(
            RequestLog.profile_id == profile_id,
            RequestLog.created_at < cutoff,
        )

    result = await db.execute(stmt)
    await db.flush()
    rowcount = getattr(result, "rowcount", 0)
    return BatchDeleteResponse(deleted_count=int(rowcount or 0))


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
    normalized_from_time = _normalize_datetime_filter(from_time)
    normalized_to_time = _normalize_datetime_filter(to_time)

    result = await get_throughput_stats(
        db,
        profile_id=profile_id,
        from_time=normalized_from_time,
        to_time=normalized_to_time,
        model_id=model_id,
        provider_type=provider_type,
        endpoint_id=endpoint_id,
        connection_id=connection_id,
    )

    return ThroughputStatsResponse(**result)
