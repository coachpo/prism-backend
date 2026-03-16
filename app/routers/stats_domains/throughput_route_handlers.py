from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.schemas import ThroughputStatsResponse
from app.services.stats_service import get_throughput_stats

from .helpers import normalize_datetime_filter


async def get_throughput(
    db: AsyncSession,
    profile_id: int,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    model_id: str | None = None,
    provider_type: str | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
    *,
    get_throughput_stats_fn=get_throughput_stats,
):
    normalized_from_time = normalize_datetime_filter(from_time)
    normalized_to_time = normalize_datetime_filter(to_time)

    result = await get_throughput_stats_fn(
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


__all__ = ["get_throughput"]
