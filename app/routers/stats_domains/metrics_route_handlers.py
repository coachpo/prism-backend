from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.schemas import (
    ConnectionMetricsBatchItem,
    ConnectionMetricsBatchRequest,
    ConnectionMetricsBatchResponse,
    ModelMetricsBatchItem,
    ModelMetricsBatchRequest,
    ModelMetricsBatchResponse,
)
from app.services.stats_service import (
    get_connection_metrics_batch,
    get_model_metrics_batch,
)

from .helpers import coerce_float, coerce_int


async def model_metrics_batch(
    body: ModelMetricsBatchRequest,
    db: AsyncSession,
    profile_id: int,
    *,
    get_model_metrics_batch_fn=get_model_metrics_batch,
):
    items = await get_model_metrics_batch_fn(
        db,
        profile_id=profile_id,
        model_ids=body.model_ids,
        summary_window_hours=body.summary_window_hours,
        spending_preset=body.spending_preset,
    )

    def build_item(model_id: str) -> ModelMetricsBatchItem:
        metric_values = items.get(model_id, {})
        return ModelMetricsBatchItem(
            model_id=model_id,
            success_rate=coerce_float(metric_values.get("success_rate")) or 0.0,
            request_count_24h=coerce_int(metric_values.get("request_count_24h")) or 0,
            p95_latency_ms=coerce_int(metric_values.get("p95_latency_ms")) or 0,
            spend_30d_micros=coerce_int(metric_values.get("spend_30d_micros")) or 0,
        )

    return ModelMetricsBatchResponse(
        items=[build_item(model_id) for model_id in body.model_ids]
    )


async def connection_metrics_batch(
    body: ConnectionMetricsBatchRequest,
    db: AsyncSession,
    profile_id: int,
    *,
    get_connection_metrics_batch_fn=get_connection_metrics_batch,
):
    items = await get_connection_metrics_batch_fn(
        db,
        profile_id=profile_id,
        model_id=body.model_id,
        connection_ids=body.connection_ids,
        summary_window_hours=body.summary_window_hours,
    )

    def build_item(connection_id: int) -> ConnectionMetricsBatchItem:
        metric_values = items.get(connection_id, {})
        last_failover_like_at = metric_values.get("last_failover_like_at")

        return ConnectionMetricsBatchItem(
            connection_id=connection_id,
            success_rate_24h=coerce_float(metric_values.get("success_rate_24h")),
            request_count_24h=coerce_int(metric_values.get("request_count_24h")) or 0,
            p95_latency_ms=coerce_int(metric_values.get("p95_latency_ms")),
            five_xx_rate=coerce_float(metric_values.get("five_xx_rate")),
            heuristic_failover_events=(
                coerce_int(metric_values.get("heuristic_failover_events")) or 0
            ),
            last_failover_like_at=(
                last_failover_like_at
                if isinstance(last_failover_like_at, datetime)
                else None
            ),
        )

    return ConnectionMetricsBatchResponse(
        items=[build_item(connection_id) for connection_id in body.connection_ids]
    )


__all__ = ["connection_metrics_batch", "model_metrics_batch"]
