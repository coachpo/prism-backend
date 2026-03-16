from datetime import datetime

from sqlalchemy import and_, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import RequestLog


async def get_throughput_stats(
    db: AsyncSession,
    *,
    profile_id: int,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    model_id: str | None = None,
    provider_type: str | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
) -> dict:
    # Build filters
    filters = [RequestLog.profile_id == profile_id]
    if from_time is not None:
        filters.append(RequestLog.created_at >= from_time)
    if to_time is not None:
        filters.append(RequestLog.created_at <= to_time)
    if model_id:
        filters.append(RequestLog.model_id == model_id)
    if provider_type:
        filters.append(RequestLog.provider_type == provider_type)
    if endpoint_id is not None:
        filters.append(RequestLog.endpoint_id == endpoint_id)
    if connection_id is not None:
        filters.append(RequestLog.connection_id == connection_id)

    filter_clause = and_(*filters) if filters else literal(True)

    # Query with 1-minute time buckets using DATE_TRUNC
    # PostgreSQL: DATE_TRUNC('minute', created_at)
    bucket_query = (
        select(
            func.date_trunc("minute", RequestLog.created_at).label("bucket_time"),
            func.count().label("request_count"),
        )
        .where(filter_clause)
        .group_by("bucket_time")
        .order_by("bucket_time")
    )

    result = await db.execute(bucket_query)
    rows = result.all()

    if from_time is not None and to_time is not None:
        time_window_seconds = max((to_time - from_time).total_seconds(), 0.0)
    elif rows:
        first_bucket = rows[0].bucket_time
        last_bucket = rows[-1].bucket_time
        time_window_seconds = (last_bucket - first_bucket).total_seconds() + 60
    else:
        time_window_seconds = 0.0

    if not rows:
        return {
            "average_rpm": 0.0,
            "peak_rpm": 0.0,
            "current_rpm": 0.0,
            "total_requests": 0,
            "time_window_seconds": round(time_window_seconds, 1),
            "buckets": [],
        }

    total_requests = sum(row.request_count for row in rows)

    buckets = []
    rpm_values = []
    for row in rows:
        rpm = float(row.request_count)
        rpm_values.append(rpm)
        buckets.append(
            {
                "timestamp": row.bucket_time.isoformat(),
                "request_count": row.request_count,
                "rpm": round(rpm, 3),
            }
        )

    time_window_minutes = time_window_seconds / 60.0 if time_window_seconds > 0 else 0.0
    average_rpm = (
        total_requests / time_window_minutes if time_window_minutes > 0 else 0.0
    )
    peak_rpm = max(rpm_values) if rpm_values else 0.0
    current_rpm = rpm_values[-1] if rpm_values else 0.0

    return {
        "average_rpm": round(average_rpm, 3),
        "peak_rpm": round(peak_rpm, 3),
        "current_rpm": round(current_rpm, 3),
        "total_requests": total_requests,
        "time_window_seconds": round(time_window_seconds, 1),
        "buckets": buckets,
    }
