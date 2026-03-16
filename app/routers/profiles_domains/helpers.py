from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Profile

MAX_NON_DELETED_PROFILES = 10


async def count_non_deleted_profiles(db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count(Profile.id)).where(Profile.deleted_at.is_(None))
    )
    return int(result.scalar_one())


async def ensure_profile_name_available(
    db: AsyncSession,
    *,
    profile_name: str,
    exclude_id: int | None = None,
) -> None:
    query = select(Profile).where(Profile.name == profile_name)
    if exclude_id is not None:
        query = query.where(Profile.id != exclude_id)
    existing = (await db.execute(query)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Profile with name '{profile_name}' already exists",
        )


async def load_profile_or_404(db: AsyncSession, *, profile_id: int) -> Profile:
    result = await db.execute(
        select(Profile).where(Profile.id == profile_id, Profile.deleted_at.is_(None))
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


async def load_active_profile_for_update(db: AsyncSession) -> Profile | None:
    result = await db.execute(
        select(Profile)
        .where(Profile.is_active.is_(True), Profile.deleted_at.is_(None))
        .with_for_update()
        .order_by(Profile.id.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


__all__ = [
    "MAX_NON_DELETED_PROFILES",
    "count_non_deleted_profiles",
    "ensure_profile_name_available",
    "load_active_profile_for_update",
    "load_profile_or_404",
]
