from datetime import timedelta

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.models.models import RequestLog
from app.services.stats.time_presets import resolve_time_preset


async def get_model_metrics_batch(
    db: AsyncSession,
    *,
    profile_id: int,
    model_ids: list[str],
    summary_window_hours: int = 24,
    spending_preset: str = "last_30_days",
) -> dict[str, dict[str, float | int]]:
    if not model_ids:
        return {}

    unique_model_ids = list(dict.fromkeys(model_ids))
    summary_from_time = utc_now() - timedelta(hours=summary_window_hours)
    spending_from_time, spending_to_time = resolve_time_preset(
        spending_preset, None, None
    )

    summary_filters = [
        RequestLog.profile_id == profile_id,
        RequestLog.model_id.in_(unique_model_ids),
        RequestLog.created_at >= summary_from_time,
    ]
    success_case = case(
        (RequestLog.status_code.between(200, 299), 1),
        else_=0,
    )

    summary_rows = (
        await db.execute(
            select(
                RequestLog.model_id.label("model_id"),
                func.count().label("total_requests"),
                func.coalesce(func.sum(success_case), 0).label("success_count"),
                func.percentile_cont(0.95)
                .within_group(RequestLog.response_time_ms.asc())
                .label("p95_response_time_ms"),
            )
            .where(and_(*summary_filters))
            .group_by(RequestLog.model_id)
        )
    ).all()

    spending_filters = [
        RequestLog.profile_id == profile_id,
        RequestLog.model_id.in_(unique_model_ids),
        RequestLog.success_flag == True,  # noqa: E712
    ]
    if spending_from_time is not None:
        spending_filters.append(RequestLog.created_at >= spending_from_time)
    if spending_to_time is not None:
        spending_filters.append(RequestLog.created_at <= spending_to_time)

    spend_case = case(
        (
            RequestLog.billable_flag == True,  # noqa: E712
            func.coalesce(RequestLog.total_cost_user_currency_micros, 0),
        ),
        else_=0,
    )
    spending_rows = (
        await db.execute(
            select(
                RequestLog.model_id.label("model_id"),
                func.coalesce(func.sum(spend_case), 0).label("total_cost_micros"),
            )
            .where(and_(*spending_filters))
            .group_by(RequestLog.model_id)
        )
    ).all()

    results: dict[str, dict[str, float | int]] = {
        model_id: {
            "success_rate": 0.0,
            "request_count_24h": 0,
            "p95_latency_ms": 0,
            "spend_30d_micros": 0,
        }
        for model_id in unique_model_ids
    }

    for row in summary_rows:
        total_requests = int(row.total_requests or 0)
        success_count = int(row.success_count or 0)
        success_rate = (
            round((success_count / total_requests * 100), 2)
            if total_requests > 0
            else 0.0
        )
        p95_latency_ms = row.p95_response_time_ms

        results[row.model_id] = {
            **results[row.model_id],
            "success_rate": success_rate,
            "request_count_24h": total_requests,
            "p95_latency_ms": int(round(float(p95_latency_ms or 0))),
        }

    for row in spending_rows:
        results[row.model_id] = {
            **results[row.model_id],
            "spend_30d_micros": int(row.total_cost_micros or 0),
        }

    return results
