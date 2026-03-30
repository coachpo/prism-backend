from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.routers.profiles_domains import (
    activate_profile as _activate_profile_impl,
    create_profile as _create_profile_impl,
    delete_profile as _delete_profile_impl,
    get_active_profile as _get_active_profile_impl,
    list_profiles as _list_profiles_impl,
    update_profile as _update_profile_impl,
)
from app.routers.profiles_domains.route_handlers import (
    get_profile_bootstrap as _get_profile_bootstrap_impl,
)
from app.schemas.schemas import (
    ProfileActivateRequest,
    ProfileBootstrapResponse,
    ProfileCreate,
    ProfileResponse,
    ProfileUpdate,
)
from app.services.profile_invariants import ensure_profile_invariants

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


@router.get("", response_model=list[ProfileResponse])
async def list_profiles(db: Annotated[AsyncSession, Depends(get_db)]):
    return await _list_profiles_impl(
        db,
        ensure_profile_invariants_fn=ensure_profile_invariants,
    )


@router.get("/active", response_model=ProfileResponse)
async def get_active_profile(db: Annotated[AsyncSession, Depends(get_db)]):
    return await _get_active_profile_impl(
        db,
        ensure_profile_invariants_fn=ensure_profile_invariants,
    )


@router.get("/bootstrap", response_model=ProfileBootstrapResponse)
async def get_profile_bootstrap(db: Annotated[AsyncSession, Depends(get_db)]):
    return await _get_profile_bootstrap_impl(
        db,
        ensure_profile_invariants_fn=ensure_profile_invariants,
    )


@router.post("", response_model=ProfileResponse, status_code=201)
async def create_profile(
    body: ProfileCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await _create_profile_impl(
        body,
        db,
        ensure_profile_invariants_fn=ensure_profile_invariants,
    )


@router.patch("/{profile_id}", response_model=ProfileResponse)
async def update_profile(
    profile_id: int,
    body: ProfileUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await _update_profile_impl(profile_id, body, db)


@router.post("/{profile_id}/activate", response_model=ProfileResponse)
async def activate_profile(
    profile_id: int,
    body: ProfileActivateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await _activate_profile_impl(
        profile_id,
        body,
        db,
        ensure_profile_invariants_fn=ensure_profile_invariants,
    )


@router.delete("/{profile_id}")
async def delete_profile(
    profile_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await _delete_profile_impl(profile_id, db)


__all__ = [
    "activate_profile",
    "create_profile",
    "delete_profile",
    "ensure_profile_invariants",
    "get_active_profile",
    "get_profile_bootstrap",
    "list_profiles",
    "router",
    "update_profile",
]
