from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.schemas import UsageModelStatistic
from app.services.stats_service import get_endpoint_model_statistics


async def endpoint_model_statistics(
    db: AsyncSession,
    profile_id: int,
    endpoint_id: int,
    preset: str = "1h",
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    *,
    get_endpoint_model_statistics_fn=get_endpoint_model_statistics,
):
    result = await get_endpoint_model_statistics_fn(
        db,
        profile_id=profile_id,
        endpoint_id=endpoint_id,
        preset=preset,
        from_time=from_time,
        to_time=to_time,
    )
    return [UsageModelStatistic(**row) for row in result]


__all__ = ["endpoint_model_statistics"]
