from datetime import datetime

from sqlalchemy import String, and_, case, cast, desc, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import RequestLog

from app.services.stats.time_presets import resolve_time_preset
from app.services.user_settings import get_report_currency_preferences


async def get_spending_report(
    db: AsyncSession,
    *,
    profile_id: int,
    preset: str | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    api_family: str | None = None,
    model_id: str | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
    group_by: str = "none",
    limit: int = 50,
    offset: int = 0,
    top_n: int = 5,
) -> dict[str, object]:
    from_time, to_time = resolve_time_preset(preset, from_time, to_time)

    filters = [RequestLog.profile_id == profile_id]
    if from_time is not None:
        filters.append(RequestLog.created_at >= from_time)
    if to_time is not None:
        filters.append(RequestLog.created_at <= to_time)
    if api_family:
        filters.append(RequestLog.api_family == api_family)
    if model_id:
        filters.append(RequestLog.model_id == model_id)
    if endpoint_id is not None:
        filters.append(RequestLog.endpoint_id == endpoint_id)
    if connection_id is not None:
        filters.append(RequestLog.connection_id == connection_id)
    where = and_(*filters) if filters else literal(True)
    success_where = and_(where, RequestLog.success_flag == True)  # noqa: E712

    spend_case = case(
        (
            RequestLog.billable_flag == True,  # noqa: E712
            func.coalesce(RequestLog.total_cost_user_currency_micros, 0),
        ),
        else_=0,
    )
    priced_case = case(
        (
            and_(
                RequestLog.success_flag == True,  # noqa: E712
                RequestLog.priced_flag == True,  # noqa: E712
            ),
            1,
        ),
        else_=0,
    )
    unpriced_case = case(
        (
            and_(
                RequestLog.success_flag == True,  # noqa: E712
                or_(
                    RequestLog.priced_flag == False,  # noqa: E712
                    RequestLog.priced_flag.is_(None),
                ),
            ),
            1,
        ),
        else_=0,
    )
    summary_row = (
        await db.execute(
            select(
                func.coalesce(func.sum(spend_case), 0).label("total_cost_micros"),
                func.count().label("successful_request_count"),
                func.coalesce(func.sum(priced_case), 0).label("priced_request_count"),
                func.coalesce(func.sum(unpriced_case), 0).label(
                    "unpriced_request_count"
                ),
                func.coalesce(
                    func.sum(func.coalesce(RequestLog.input_tokens, 0)), 0
                ).label("total_input_tokens"),
                func.coalesce(
                    func.sum(func.coalesce(RequestLog.output_tokens, 0)), 0
                ).label("total_output_tokens"),
                func.coalesce(
                    func.sum(func.coalesce(RequestLog.cache_read_input_tokens, 0)), 0
                ).label("total_cache_read_input_tokens"),
                func.coalesce(
                    func.sum(func.coalesce(RequestLog.cache_creation_input_tokens, 0)),
                    0,
                ).label("total_cache_creation_input_tokens"),
                func.coalesce(
                    func.sum(func.coalesce(RequestLog.reasoning_tokens, 0)), 0
                ).label("total_reasoning_tokens"),
                func.coalesce(
                    func.sum(func.coalesce(RequestLog.total_tokens, 0)), 0
                ).label("total_tokens"),
            ).where(success_where)
        )
    ).one()

    successful_request_count = int(summary_row.successful_request_count or 0)
    total_cost_micros = int(summary_row.total_cost_micros or 0)
    avg_cost_per_success = (
        int(total_cost_micros / successful_request_count)
        if successful_request_count > 0
        else 0
    )

    group_expr = None
    if group_by == "day":
        group_expr = func.to_char(RequestLog.created_at, "YYYY-MM-DD")
    elif group_by == "week":
        group_expr = func.to_char(
            func.date_trunc("week", RequestLog.created_at), 'IYYY-"W"IW'
        )
    elif group_by == "month":
        group_expr = func.to_char(RequestLog.created_at, "YYYY-MM")
    elif group_by == "api_family":
        group_expr = RequestLog.api_family
    elif group_by == "model":
        group_expr = RequestLog.model_id
    elif group_by == "endpoint":
        group_expr = func.coalesce(
            RequestLog.endpoint_description,
            RequestLog.endpoint_base_url,
            literal("unknown_endpoint"),
        )
    elif group_by == "model_endpoint":
        group_expr = func.concat(
            RequestLog.model_id,
            literal("#"),
            func.coalesce(cast(RequestLog.endpoint_id, String), literal("-1")),
        )

    groups: list[dict[str, int | str]] = []
    groups_total = 0
    if group_expr is not None:
        groups_total = (
            await db.execute(
                select(func.count()).select_from(
                    select(group_expr.label("group_key"))
                    .where(success_where)
                    .group_by(group_expr)
                    .subquery()
                )
            )
        ).scalar_one()

        grouped_rows = (
            await db.execute(
                select(
                    group_expr.label("key"),
                    func.count().label("total_requests"),
                    func.coalesce(func.sum(priced_case), 0).label("priced_requests"),
                    func.coalesce(func.sum(unpriced_case), 0).label(
                        "unpriced_requests"
                    ),
                    func.coalesce(
                        func.sum(func.coalesce(RequestLog.total_tokens, 0)), 0
                    ).label("total_tokens"),
                    func.coalesce(func.sum(spend_case), 0).label("total_cost_micros"),
                )
                .where(success_where)
                .group_by(group_expr)
                .order_by(desc(func.coalesce(func.sum(spend_case), 0)))
                .limit(limit)
                .offset(offset)
            )
        ).all()

        groups = [
            {
                "key": row.key or "unknown",
                "total_cost_micros": int(row.total_cost_micros or 0),
                "total_requests": int(row.total_requests or 0),
                "priced_requests": int(row.priced_requests or 0),
                "unpriced_requests": int(row.unpriced_requests or 0),
                "total_tokens": int(row.total_tokens or 0),
            }
            for row in grouped_rows
        ]
    else:
        groups = [
            {
                "key": "all",
                "total_cost_micros": total_cost_micros,
                "total_requests": successful_request_count,
                "priced_requests": int(summary_row.priced_request_count or 0),
                "unpriced_requests": int(summary_row.unpriced_request_count or 0),
                "total_tokens": int(summary_row.total_tokens or 0),
            }
        ]
        groups_total = 1

    top_model_rows = (
        await db.execute(
            select(
                RequestLog.model_id,
                func.coalesce(func.sum(spend_case), 0).label("total_cost_micros"),
            )
            .where(success_where)
            .group_by(RequestLog.model_id)
            .having(func.coalesce(func.sum(spend_case), 0) > 0)
            .order_by(desc(func.coalesce(func.sum(spend_case), 0)))
            .limit(top_n)
        )
    ).all()

    endpoint_label_expr = func.coalesce(
        RequestLog.endpoint_description,
        RequestLog.endpoint_base_url,
        literal("unknown_endpoint"),
    )
    top_endpoint_rows = (
        await db.execute(
            select(
                RequestLog.endpoint_id,
                endpoint_label_expr.label("endpoint_label"),
                func.coalesce(func.sum(spend_case), 0).label("total_cost_micros"),
            )
            .where(success_where)
            .group_by(RequestLog.endpoint_id, endpoint_label_expr)
            .having(func.coalesce(func.sum(spend_case), 0) > 0)
            .order_by(desc(func.coalesce(func.sum(spend_case), 0)))
            .limit(top_n)
        )
    ).all()

    unpriced_reason_rows = (
        await db.execute(
            select(
                RequestLog.unpriced_reason.label("reason"),
                func.count().label("reason_count"),
            )
            .where(
                success_where,
                or_(
                    RequestLog.priced_flag == False,  # noqa: E712
                    RequestLog.priced_flag.is_(None),
                ),
            )
            .group_by("reason")
            .order_by(desc(func.count()))
        )
    ).all()

    (
        report_currency_code,
        report_currency_symbol,
    ) = await get_report_currency_preferences(
        db,
        profile_id=profile_id,
    )

    unpriced_breakdown: dict[str, int] = {}
    for row in unpriced_reason_rows:
        reason = row[0]
        reason_count = row[1]
        key = str(reason or "UNKNOWN")
        unpriced_breakdown[key] = unpriced_breakdown.get(key, 0) + int(
            reason_count or 0
        )

    return {
        "summary": {
            "total_cost_micros": total_cost_micros,
            "successful_request_count": successful_request_count,
            "priced_request_count": int(summary_row.priced_request_count or 0),
            "unpriced_request_count": int(summary_row.unpriced_request_count or 0),
            "total_input_tokens": int(summary_row.total_input_tokens or 0),
            "total_output_tokens": int(summary_row.total_output_tokens or 0),
            "total_cache_read_input_tokens": int(
                summary_row.total_cache_read_input_tokens or 0
            ),
            "total_cache_creation_input_tokens": int(
                summary_row.total_cache_creation_input_tokens or 0
            ),
            "total_reasoning_tokens": int(summary_row.total_reasoning_tokens or 0),
            "total_tokens": int(summary_row.total_tokens or 0),
            "avg_cost_per_successful_request_micros": avg_cost_per_success,
        },
        "groups": groups,
        "groups_total": int(groups_total or 0),
        "top_spending_models": [
            {
                "model_id": row.model_id,
                "total_cost_micros": int(row.total_cost_micros or 0),
            }
            for row in top_model_rows
        ],
        "top_spending_endpoints": [
            {
                "endpoint_id": row.endpoint_id,
                "endpoint_label": row.endpoint_label,
                "total_cost_micros": int(row.total_cost_micros or 0),
            }
            for row in top_endpoint_rows
        ],
        "unpriced_breakdown": unpriced_breakdown,
        "report_currency_code": report_currency_code,
        "report_currency_symbol": report_currency_symbol,
    }
