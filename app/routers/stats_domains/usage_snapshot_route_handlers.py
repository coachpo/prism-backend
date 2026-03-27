from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.schemas import UsageSnapshotResponse
from app.services.stats_service import get_usage_snapshot


async def usage_snapshot(
    db: AsyncSession,
    profile_id: int,
    preset: str = "24h",
    *,
    get_usage_snapshot_fn=get_usage_snapshot,
):
    result = await get_usage_snapshot_fn(
        db,
        profile_id=profile_id,
        preset=preset,
    )
    return UsageSnapshotResponse(**result)


__all__ = ["usage_snapshot"]
