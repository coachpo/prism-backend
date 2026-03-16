from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Profile


async def lock_profile_row(db: AsyncSession, *, profile_id: int) -> None:
    await db.execute(
        select(Profile.id).where(Profile.id == profile_id).with_for_update()
    )


__all__ = ["lock_profile_row"]
