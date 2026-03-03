from __future__ import annotations

from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.models.models import Profile

DEFAULT_PROFILE_NAME: Final[str] = "Default"
DEFAULT_PROFILE_DESCRIPTION: Final[str] = "System default profile"


async def ensure_default_profile(session: AsyncSession) -> Profile:
    result = await session.execute(
        select(Profile).where(Profile.is_default.is_(True)).order_by(Profile.id.asc()).limit(1)
    )
    default_profile = result.scalar_one_or_none()

    if default_profile is not None:
        changed = False
        if default_profile.deleted_at is not None:
            default_profile.deleted_at = None
            changed = True
        if default_profile.name != DEFAULT_PROFILE_NAME:
            default_profile.name = DEFAULT_PROFILE_NAME
            changed = True
        if not default_profile.is_default:
            default_profile.is_default = True
            changed = True
        if not default_profile.is_editable:
            default_profile.is_editable = True
            changed = True
        if changed:
            default_profile.updated_at = utc_now()
            default_profile.version += 1
            await session.flush()
        await session.refresh(default_profile)
        return default_profile

    default_profile = Profile(
        name=DEFAULT_PROFILE_NAME,
        description=DEFAULT_PROFILE_DESCRIPTION,
        is_active=False,
        is_default=True,
        is_editable=True,
        version=0,
    )
    session.add(default_profile)
    await session.flush()
    await session.refresh(default_profile)
    return default_profile


async def ensure_profile_invariants(session: AsyncSession) -> Profile:
    default_profile = await ensure_default_profile(session)

    active_result = await session.execute(
        select(Profile)
        .where(Profile.is_active.is_(True), Profile.deleted_at.is_(None))
        .order_by(Profile.id.asc())
        .limit(1)
    )
    active_profile = active_result.scalar_one_or_none()

    if active_profile is None:
        if not default_profile.is_active:
            default_profile.is_active = True
            default_profile.updated_at = utc_now()
            default_profile.version += 1
            await session.flush()
            await session.refresh(default_profile)
        return default_profile

    if active_profile.id != default_profile.id and default_profile.is_active:
        default_profile.is_active = False
        default_profile.updated_at = utc_now()
        default_profile.version += 1
        await session.flush()
        await session.refresh(default_profile)
    return active_profile
