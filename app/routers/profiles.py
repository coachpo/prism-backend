from typing import Annotated
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.models.models import Profile
from app.schemas.schemas import (
    ProfileCreate,
    ProfileUpdate,
    ProfileResponse,
    ProfileActivateRequest,
)

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


@router.get("", response_model=list[ProfileResponse])
async def list_profiles(db: Annotated[AsyncSession, Depends(get_db)]):
    """List all non-deleted profiles."""
    result = await db.execute(
        select(Profile).where(Profile.deleted_at.is_(None)).order_by(Profile.id.asc())
    )
    return result.scalars().all()


@router.get("/active", response_model=ProfileResponse)
async def get_active_profile(db: Annotated[AsyncSession, Depends(get_db)]):
    """Get the currently active profile."""
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


@router.post("", response_model=ProfileResponse, status_code=201)
async def create_profile(
    body: ProfileCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new profile. Maximum 10 non-deleted profiles allowed."""
    # Check capacity: count non-deleted profiles
    count_result = await db.execute(
        select(func.count(Profile.id)).where(Profile.deleted_at.is_(None))
    )
    non_deleted_count = count_result.scalar_one()

    if non_deleted_count >= 10:
        raise HTTPException(
            status_code=409,
            detail="Maximum 10 profiles reached. Delete a profile to create a new one.",
        )

    # Check for duplicate name
    existing = await db.execute(select(Profile).where(Profile.name == body.name))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Profile with name '{body.name}' already exists",
        )

    profile = Profile(
        name=body.name,
        description=body.description,
        is_active=False,
        version=0,
    )
    db.add(profile)
    await db.flush()
    await db.refresh(profile)
    return profile


@router.patch("/{profile_id}", response_model=ProfileResponse)
async def update_profile(
    profile_id: int,
    body: ProfileUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update profile name and/or description."""
    result = await db.execute(
        select(Profile).where(Profile.id == profile_id, Profile.deleted_at.is_(None))
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    update_data = body.model_dump(exclude_unset=True)

    # Check for name conflict if name is being updated
    if "name" in update_data and update_data["name"] != profile.name:
        existing = await db.execute(
            select(Profile).where(Profile.name == update_data["name"])
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Profile with name '{update_data['name']}' already exists",
            )

    for key, value in update_data.items():
        setattr(profile, key, value)

    profile.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(profile)
    return profile


@router.post("/{profile_id}/activate", response_model=ProfileResponse)
async def activate_profile(
    profile_id: int,
    body: ProfileActivateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Activate a profile using CAS (Compare-And-Swap) for conflict-safe activation.

    The request must include the expected current active profile ID and version.
    If the current active profile doesn't match expectations, returns 409 Conflict.
    """
    # Serialize activation attempts on the currently-active row so stale CAS payloads
    # deterministically become 409 conflicts rather than uniqueness races.
    active_result = await db.execute(
        select(Profile)
        .where(Profile.is_active.is_(True), Profile.deleted_at.is_(None))
        .with_for_update()
        .order_by(Profile.id.asc())
        .limit(1)
    )
    current_active = active_result.scalar_one_or_none()

    # CAS validation
    if current_active is None:
        raise HTTPException(
            status_code=503,
            detail="No active profile configured",
        )

    if current_active.id != body.expected_active_profile_id:
        raise HTTPException(
            status_code=409,
            detail=f"Active profile mismatch: expected {body.expected_active_profile_id}, got {current_active.id}",
        )

    if current_active.version != body.expected_active_profile_version:
        raise HTTPException(
            status_code=409,
            detail=f"Active profile version mismatch: expected {body.expected_active_profile_version}, got {current_active.version}",
        )

    # If target is already active, no-op
    if profile_id == current_active.id:
        return current_active

    # Lock and validate target after CAS checks.
    target_result = await db.execute(
        select(Profile)
        .where(Profile.id == profile_id, Profile.deleted_at.is_(None))
        .with_for_update()
        .limit(1)
    )
    target_profile = target_result.scalar_one_or_none()
    if target_profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    # Perform atomic switch
    current_active.is_active = False
    current_active.version += 1
    current_active.updated_at = datetime.utcnow()

    target_profile.is_active = True
    target_profile.version += 1
    target_profile.updated_at = datetime.utcnow()

    await db.flush()
    await db.refresh(target_profile)
    return target_profile


@router.delete("/{profile_id}", status_code=204)
async def delete_profile(
    profile_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Soft-delete an inactive profile.

    Active profiles cannot be deleted and will return 400 Bad Request.
    """
    result = await db.execute(
        select(Profile).where(Profile.id == profile_id, Profile.deleted_at.is_(None))
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    if profile.is_active:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete active profile. Activate another profile first.",
        )

    # Soft delete
    profile.deleted_at = datetime.utcnow()
    profile.updated_at = datetime.utcnow()
    await db.flush()
    return None
