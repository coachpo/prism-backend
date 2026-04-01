from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.schemas import (
    ModelMetricsBatchItem,
    ModelMetricsBatchRequest,
    ModelMetricsBatchResponse,
)
from app.services.stats_service import get_model_metrics_batch

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


__all__ = ["model_metrics_batch"]
