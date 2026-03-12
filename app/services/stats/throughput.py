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
    """
    Compute TPS/QPS metrics with time-bucketed aggregation.

    Returns:
        dict with keys:
            - average_tps: float
            - peak_tps: float
            - current_tps: float
            - total_requests: int
            - time_window_seconds: float
            - buckets: list[dict] with timestamp, request_count, tps
    """
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

    # Compute metrics
    if not rows:
        return {
            "average_tps": 0.0,
            "peak_tps": 0.0,
            "current_tps": 0.0,
            "total_requests": 0,
            "time_window_seconds": 0.0,
            "buckets": [],
        }

    total_requests = sum(row.request_count for row in rows)

    # Calculate time window in seconds
    if from_time and to_time:
        time_window_seconds = (to_time - from_time).total_seconds()
    elif rows:
        # Use actual data range
        first_bucket = rows[0].bucket_time
        last_bucket = rows[-1].bucket_time
        time_window_seconds = (
            last_bucket - first_bucket
        ).total_seconds() + 60  # +60 for last bucket
    else:
        time_window_seconds = 0.0

    # Compute TPS per bucket (requests per 60 seconds)
    buckets = []
    tps_values = []
    for row in rows:
        tps = row.request_count / 60.0  # 1-minute bucket = 60 seconds
        tps_values.append(tps)
        buckets.append(
            {
                "timestamp": row.bucket_time.isoformat(),
                "request_count": row.request_count,
                "tps": round(tps, 3),
            }
        )

    # Aggregate metrics
    average_tps = (
        total_requests / time_window_seconds if time_window_seconds > 0 else 0.0
    )
    peak_tps = max(tps_values) if tps_values else 0.0
    current_tps = tps_values[-1] if tps_values else 0.0

    return {
        "average_tps": round(average_tps, 3),
        "peak_tps": round(peak_tps, 3),
        "current_tps": round(current_tps, 3),
        "total_requests": total_requests,
        "time_window_seconds": round(time_window_seconds, 1),
        "buckets": buckets,
    }
