from datetime import datetime

from fastapi import Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.stats_service import get_spending_report

from .helpers import normalize_datetime_filter


async def spending_report(
    db: AsyncSession,
    profile_id: int,
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
    limit: int = 50,
    offset: int = 0,
    top_n: int = 5,
    *,
    get_spending_report_fn=get_spending_report,
):
    normalized_from_time = normalize_datetime_filter(from_time)
    normalized_to_time = normalize_datetime_filter(to_time)

    return await get_spending_report_fn(
        db,
        preset=preset,
        from_time=normalized_from_time,
        profile_id=profile_id,
        to_time=normalized_to_time,
        api_family=api_family,
        model_id=model_id,
        endpoint_id=endpoint_id,
        connection_id=connection_id,
        group_by=group_by,
        limit=limit,
        offset=offset,
        top_n=top_n,
    )


__all__ = ["spending_report"]
