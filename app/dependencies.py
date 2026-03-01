import asyncio
import logging
from typing import Annotated, AsyncGenerator

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.models import Profile

logger = logging.getLogger(__name__)


PROFILE_ID_HEADER = "X-Profile-Id"


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except asyncio.CancelledError:
            await session.rollback()
            raise
        except Exception:
            await session.rollback()
            raise


async def _get_non_deleted_profile(
    db: AsyncSession,
    *,
    profile_id: int,
 ) -> Profile | None:
    result = await db.execute(
        select(Profile)
        .where(Profile.id == profile_id, Profile.deleted_at.is_(None))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_active_profile(
    db: Annotated[AsyncSession, Depends(get_db)],
 ) -> Profile:
    result = await db.execute(
        select(Profile)
        .where(Profile.is_active.is_(True), Profile.deleted_at.is_(None))
        .order_by(Profile.id.asc())
        .limit(1)
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        raise HTTPException(status_code=503, detail="No active profile configured")
    return profile


async def get_effective_profile(
    db: Annotated[AsyncSession, Depends(get_db)],
    x_profile_id: Annotated[str | None, Header(alias=PROFILE_ID_HEADER)] = None,
 ) -> Profile:
    if x_profile_id is None:
        raise HTTPException(
            status_code=400, detail=f"{PROFILE_ID_HEADER} header is required"
        )
    try:
        profile_id = int(x_profile_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"{PROFILE_ID_HEADER} must be an integer"
        ) from exc

    if profile_id <= 0:
        raise HTTPException(
            status_code=400, detail=f"{PROFILE_ID_HEADER} must be a positive integer"
        )

    profile = await _get_non_deleted_profile(db, profile_id=profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"Profile {profile_id} not found")
    return profile


async def get_active_profile_id(
    profile: Annotated[Profile, Depends(get_active_profile)],
 ) -> int:
    return profile.id


async def get_effective_profile_id(
    profile: Annotated[Profile, Depends(get_effective_profile)],
 ) -> int:
    return profile.id
