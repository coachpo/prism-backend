from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Endpoint


async def ensure_unique_endpoint_name(
    db: AsyncSession,
    *,
    profile_id: int,
    endpoint_name: str,
    exclude_id: int | None = None,
) -> None:
    query = select(Endpoint).where(
        Endpoint.profile_id == profile_id,
        Endpoint.name == endpoint_name,
    )
    if exclude_id is not None:
        query = query.where(Endpoint.id != exclude_id)
    existing = (await db.execute(query)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Endpoint name '{endpoint_name}' already exists",
        )


async def get_next_endpoint_position(db: AsyncSession, *, profile_id: int) -> int:
    result = await db.execute(
        select(func.max(Endpoint.position)).where(Endpoint.profile_id == profile_id)
    )
    max_position = result.scalar_one_or_none()
    if max_position is None:
        return 0
    return int(max_position) + 1


__all__ = ["ensure_unique_endpoint_name", "get_next_endpoint_position"]
