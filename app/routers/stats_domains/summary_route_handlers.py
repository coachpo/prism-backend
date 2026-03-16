from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.schemas import StatsSummaryResponse
from app.services.stats_service import get_connection_success_rates, get_stats_summary

from .helpers import normalize_datetime_filter


async def stats_summary(
    db: AsyncSession,
    profile_id: int,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    group_by: str | None = None,
    model_id: str | None = None,
    provider_type: str | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
    *,
    get_stats_summary_fn=get_stats_summary,
):
    normalized_from_time = normalize_datetime_filter(from_time)
    normalized_to_time = normalize_datetime_filter(to_time)

    result = await get_stats_summary_fn(
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


async def connection_success_rates(
    db: AsyncSession,
    profile_id: int,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    *,
    get_connection_success_rates_fn=get_connection_success_rates,
):
    normalized_from_time = normalize_datetime_filter(from_time)
    normalized_to_time = normalize_datetime_filter(to_time)

    return await get_connection_success_rates_fn(
        db,
        profile_id=profile_id,
        from_time=normalized_from_time,
        to_time=normalized_to_time,
    )


__all__ = ["connection_success_rates", "stats_summary"]
