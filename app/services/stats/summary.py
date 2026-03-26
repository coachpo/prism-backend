from datetime import datetime

from sqlalchemy import and_, case, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import RequestLog


async def get_stats_summary(
    db: AsyncSession,
    *,
    profile_id: int,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    group_by: str | None = None,
    model_id: str | None = None,
    api_family: str | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
) -> dict[str, object]:
    time_filters = [RequestLog.profile_id == profile_id]
    if from_time is not None:
        time_filters.append(RequestLog.created_at >= from_time)
    if to_time is not None:
        time_filters.append(RequestLog.created_at <= to_time)
    if model_id:
        time_filters.append(RequestLog.model_id == model_id)
    if api_family:
        time_filters.append(RequestLog.api_family == api_family)
    if endpoint_id is not None:
        time_filters.append(RequestLog.endpoint_id == endpoint_id)
    if connection_id is not None:
        time_filters.append(RequestLog.connection_id == connection_id)

    time_filter = and_(*time_filters) if time_filters else literal(True)

    success_case = case(
        (RequestLog.status_code.between(200, 299), 1),
        else_=0,
    )

    agg_q = select(
        func.count().label("total_requests"),
        func.sum(success_case).label("success_count"),
        func.avg(RequestLog.response_time_ms).label("avg_response_time_ms"),
        func.coalesce(
            func.percentile_cont(0.95).within_group(RequestLog.response_time_ms.asc()),
            0,
        ).label("p95_response_time_ms"),
        func.coalesce(func.sum(RequestLog.input_tokens), 0).label("total_input_tokens"),
        func.coalesce(func.sum(RequestLog.output_tokens), 0).label(
            "total_output_tokens"
        ),
        func.coalesce(func.sum(RequestLog.total_tokens), 0).label("total_tokens"),
    ).where(time_filter)

    row = (await db.execute(agg_q)).one()
    total_requests = row.total_requests or 0
    success_count = row.success_count or 0
    error_count = total_requests - success_count
    success_rate = (
        round((success_count / total_requests * 100), 2) if total_requests > 0 else 0.0
    )
    avg_rt = round(row.avg_response_time_ms or 0, 1)
    p95 = int(row.p95_response_time_ms or 0)

    groups = []
    if group_by in ("model", "api_family", "endpoint"):
        col_map = {
            "model": RequestLog.model_id,
            "api_family": RequestLog.api_family,
            "endpoint": RequestLog.endpoint_base_url,
        }
        group_col = col_map[group_by]
        grp_q = (
            select(
                group_col.label("key"),
                func.count().label("total_requests"),
                func.sum(success_case).label("success_count"),
                func.avg(RequestLog.response_time_ms).label("avg_response_time_ms"),
                func.coalesce(func.sum(RequestLog.total_tokens), 0).label(
                    "total_tokens"
                ),
            )
            .where(time_filter)
            .group_by(group_col)
            .order_by(func.count().desc())
        )
        grp_rows = (await db.execute(grp_q)).all()
        for g in grp_rows:
            g_total = g.total_requests or 0
            g_success = g.success_count or 0
            groups.append(
                {
                    "key": g.key or "unknown",
                    "total_requests": g_total,
                    "success_count": g_success,
                    "error_count": g_total - g_success,
                    "avg_response_time_ms": round(g.avg_response_time_ms or 0, 1),
                    "total_tokens": g.total_tokens or 0,
                }
            )

    return {
        "total_requests": total_requests,
        "success_count": success_count,
        "error_count": error_count,
        "success_rate": success_rate,
        "avg_response_time_ms": avg_rt,
        "p95_response_time_ms": p95,
        "total_input_tokens": row.total_input_tokens or 0,
        "total_output_tokens": row.total_output_tokens or 0,
        "total_tokens": row.total_tokens or 0,
        "groups": groups,
    }


async def get_connection_success_rates(
    db: AsyncSession,
    *,
    profile_id: int,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
) -> list[dict[str, int | float | None]]:
    time_filters = [RequestLog.profile_id == profile_id]
    if from_time is not None:
        time_filters.append(RequestLog.created_at >= from_time)
    if to_time is not None:
        time_filters.append(RequestLog.created_at <= to_time)

    success_case = case(
        (RequestLog.status_code.between(200, 299), 1),
        else_=0,
    )

    q = (
        select(
            RequestLog.connection_id.label("connection_id"),
            func.count().label("total_requests"),
            func.sum(success_case).label("success_count"),
        )
        .where(RequestLog.connection_id.isnot(None))
        .group_by(RequestLog.connection_id)
    )
    if time_filters:
        q = q.where(and_(*time_filters))

    rows = (await db.execute(q)).all()
    results = []
    for row in rows:
        total = row.total_requests or 0
        success = row.success_count or 0
        error = total - success
        rate = round((success / total * 100), 2) if total > 0 else None
        results.append(
            {
                "connection_id": row.connection_id,
                "total_requests": total,
                "success_count": success,
                "error_count": error,
                "success_rate": rate,
            }
        )
    return results


async def get_endpoint_success_rates(
    db: AsyncSession,
    *,
    profile_id: int,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
) -> list[dict[str, int | float | None]]:
    return await get_connection_success_rates(
        db,
        profile_id=profile_id,
        from_time=from_time,
        to_time=to_time,
    )


async def get_model_health_stats(
    db: AsyncSession,
    *,
    profile_id: int,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
) -> dict[str, dict[str, int | float | None]]:
    time_filters = [RequestLog.profile_id == profile_id]
    if from_time is not None:
        time_filters.append(RequestLog.created_at >= from_time)
    if to_time is not None:
        time_filters.append(RequestLog.created_at <= to_time)

    success_case = case(
        (RequestLog.status_code.between(200, 299), 1),
        else_=0,
    )

    q = select(
        RequestLog.model_id.label("model_id"),
        func.count().label("total_requests"),
        func.sum(success_case).label("success_count"),
    ).group_by(RequestLog.model_id)
    if time_filters:
        q = q.where(and_(*time_filters))

    rows = (await db.execute(q)).all()
    result = {}
    for row in rows:
        total = row.total_requests or 0
        success = row.success_count or 0
        rate = round((success / total * 100), 2) if total > 0 else None
        result[row.model_id] = {
            "health_success_rate": rate,
            "health_total_requests": total,
        }
    return result
