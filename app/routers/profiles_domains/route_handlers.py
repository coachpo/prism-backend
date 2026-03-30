from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.models.models import Profile, UserSetting
from app.schemas.schemas import (
    ProfileActivateRequest,
    ProfileBootstrapResponse,
    ProfileCreate,
    ProfileLimitsResponse,
    ProfileResponse,
    ProfileUpdate,
)
from app.services.profile_invariants import (
    DEFAULT_PROFILE_NAME,
    ensure_profile_invariants,
)

from .helpers import (
    MAX_NON_DELETED_PROFILES,
    count_non_deleted_profiles,
    ensure_profile_name_available,
    load_active_profile_for_update,
    load_profile_or_404,
)


async def list_profiles(
    db: AsyncSession,
    *,
    ensure_profile_invariants_fn=ensure_profile_invariants,
):
    await ensure_profile_invariants_fn(db)
    result = await db.execute(
        select(Profile).where(Profile.deleted_at.is_(None)).order_by(Profile.id.asc())
    )
    return result.scalars().all()


async def get_active_profile(
    db: AsyncSession,
    *,
    ensure_profile_invariants_fn=ensure_profile_invariants,
):
    return await ensure_profile_invariants_fn(db)


async def get_profile_bootstrap(
    db: AsyncSession,
    *,
    ensure_profile_invariants_fn=ensure_profile_invariants,
):
    active_profile = await ensure_profile_invariants_fn(db)
    result = await db.execute(
        select(Profile).where(Profile.deleted_at.is_(None)).order_by(Profile.id.asc())
    )
    profiles = [
        ProfileResponse.model_validate(profile, from_attributes=True)
        for profile in result.scalars().all()
    ]
    active_profile_response = (
        None
        if active_profile is None
        else ProfileResponse.model_validate(active_profile, from_attributes=True)
    )

    return ProfileBootstrapResponse(
        profiles=profiles,
        active_profile=active_profile_response,
        profile_limits=ProfileLimitsResponse(max_profiles=MAX_NON_DELETED_PROFILES),
    )


async def create_profile(
    body: ProfileCreate,
    db: AsyncSession,
    *,
    ensure_profile_invariants_fn=ensure_profile_invariants,
):
    non_deleted_count = await count_non_deleted_profiles(db)
    if non_deleted_count >= MAX_NON_DELETED_PROFILES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Maximum {MAX_NON_DELETED_PROFILES} profiles reached. "
                "Delete a profile to create a new one."
            ),
        )

    await ensure_profile_name_available(db, profile_name=body.name)
    await ensure_profile_invariants_fn(db)

    profile = Profile(
        name=body.name,
        description=body.description,
        is_active=False,
        is_default=False,
        is_editable=True,
        version=0,
    )
    settings = UserSetting(
        profile=profile,
        report_currency_code="USD",
        report_currency_symbol="$",
        timezone_preference=None,
    )
    db.add(profile)
    db.add(settings)
    await db.flush()
    await db.refresh(profile)
    return profile


async def update_profile(
    profile_id: int,
    body: ProfileUpdate,
    db: AsyncSession,
):
    profile = await load_profile_or_404(db, profile_id=profile_id)

    if profile.is_default and not profile.is_editable:
        raise HTTPException(
            status_code=400,
            detail="Default profile is locked and cannot be modified.",
        )

    update_data = body.model_dump(exclude_unset=True)

    if (
        profile.is_default
        and "name" in update_data
        and update_data["name"] != DEFAULT_PROFILE_NAME
    ):
        raise HTTPException(
            status_code=400,
            detail="Default profile name is immutable.",
        )

    if "name" in update_data and update_data["name"] != profile.name:
        await ensure_profile_name_available(
            db,
            profile_name=update_data["name"],
            exclude_id=profile.id,
        )

    if (
        "name" in update_data
        and update_data["name"] == DEFAULT_PROFILE_NAME
        and not profile.is_default
    ):
        raise HTTPException(
            status_code=409,
            detail=f"Profile with name '{DEFAULT_PROFILE_NAME}' already exists",
        )

    for key, value in update_data.items():
        setattr(profile, key, value)

    profile.updated_at = utc_now()
    if profile.is_default:
        profile.name = DEFAULT_PROFILE_NAME
    await db.flush()
    await db.refresh(profile)
    return profile


async def activate_profile(
    profile_id: int,
    body: ProfileActivateRequest,
    db: AsyncSession,
    *,
    ensure_profile_invariants_fn=ensure_profile_invariants,
):
    await ensure_profile_invariants_fn(db)
    current_active = await load_active_profile_for_update(db)

    if current_active is None:
        current_active = await ensure_profile_invariants_fn(db)
    if current_active.id != body.expected_active_profile_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "Active profile mismatch: expected "
                f"{body.expected_active_profile_id}, got {current_active.id}"
            ),
        )

    if profile_id == current_active.id:
        return current_active

    target_result = await db.execute(
        select(Profile)
        .where(Profile.id == profile_id, Profile.deleted_at.is_(None))
        .with_for_update()
        .limit(1)
    )
    target_profile = target_result.scalar_one_or_none()
    if target_profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    current_active.is_active = False
    current_active.version += 1
    current_active.updated_at = utc_now()
    await db.flush()

    try:
        target_profile.is_active = True
        target_profile.version += 1
        target_profile.updated_at = utc_now()
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="Profile activation conflict. Please retry.",
        ) from exc

    await db.refresh(target_profile)
    return target_profile


async def delete_profile(
    profile_id: int,
    db: AsyncSession,
):
    profile = await load_profile_or_404(db, profile_id=profile_id)

    if profile.is_default:
        raise HTTPException(
            status_code=400,
            detail="Default profile cannot be deleted.",
        )
    if profile.is_active:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete active profile. Activate another profile first.",
        )

    profile.deleted_at = utc_now()
    profile.updated_at = utc_now()
    await db.flush()
    return {"deleted": True}


__all__ = [
    "activate_profile",
    "create_profile",
    "delete_profile",
    "get_active_profile",
    "get_profile_bootstrap",
    "list_profiles",
    "update_profile",
]
