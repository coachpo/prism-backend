from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_effective_profile_id
from app.routers.endpoints_domains import (
    create_endpoint_record,
    delete_endpoint_record,
    duplicate_endpoint_record,
    list_connection_dropdown_response,
    list_endpoints_for_profile,
    move_endpoint_position_record,
    update_endpoint_record,
)
from app.schemas.schemas import (
    ConnectionDropdownResponse,
    EndpointCreate,
    EndpointPositionMoveRequest,
    EndpointResponse,
    EndpointUpdate,
)

router = APIRouter(tags=["endpoints"])


@router.get("/api/endpoints", response_model=list[EndpointResponse])
async def list_endpoints(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await list_endpoints_for_profile(db, profile_id=profile_id)


@router.post("/api/endpoints", response_model=EndpointResponse, status_code=201)
async def create_endpoint(
    body: EndpointCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await create_endpoint_record(body, db, profile_id=profile_id)


@router.patch(
    "/api/endpoints/{endpoint_id}/position",
    response_model=list[EndpointResponse],
)
async def move_endpoint_position(
    endpoint_id: int,
    body: EndpointPositionMoveRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await move_endpoint_position_record(
        endpoint_id,
        body,
        db,
        profile_id=profile_id,
    )


@router.get("/api/endpoints/connections", response_model=ConnectionDropdownResponse)
async def list_all_connections(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await list_connection_dropdown_response(db, profile_id=profile_id)


@router.put("/api/endpoints/{endpoint_id}", response_model=EndpointResponse)
async def update_endpoint(
    endpoint_id: int,
    body: EndpointUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await update_endpoint_record(
        endpoint_id,
        body,
        db,
        profile_id=profile_id,
    )


@router.post(
    "/api/endpoints/{endpoint_id}/duplicate",
    response_model=EndpointResponse,
    status_code=201,
)
async def duplicate_endpoint(
    endpoint_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await duplicate_endpoint_record(endpoint_id, db, profile_id=profile_id)


@router.delete("/api/endpoints/{endpoint_id}")
async def delete_endpoint(
    endpoint_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await delete_endpoint_record(endpoint_id, db, profile_id=profile_id)
