from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.time import utc_now
from app.dependencies import get_db, get_effective_profile_id
from app.models.models import Connection, Endpoint, Profile
from app.schemas.schemas import (
    ConnectionDropdownResponse,
    ConnectionDropdownItem,
    EndpointCreate,
    EndpointPositionMoveRequest,
    EndpointResponse,
    EndpointUpdate,
)
from app.services.loadbalancer import mark_connection_recovered
from app.services.proxy_service import normalize_base_url, validate_base_url

router = APIRouter(tags=["endpoints"])


async def _ensure_unique_endpoint_name(
    db: AsyncSession,
    *,
    profile_id: int,
    endpoint_name: str,
    exclude_id: int | None = None,
) -> None:
    query = select(Endpoint).where(
        Endpoint.profile_id == profile_id, Endpoint.name == endpoint_name
    )
    if exclude_id is not None:
        query = query.where(Endpoint.id != exclude_id)
    existing = (await db.execute(query)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Endpoint name '{endpoint_name}' already exists",
        )


async def _lock_profile_row(db: AsyncSession, *, profile_id: int) -> None:
    await db.execute(
        select(Profile.id).where(Profile.id == profile_id).with_for_update()
    )


async def _list_ordered_endpoints(
    db: AsyncSession, *, profile_id: int
) -> list[Endpoint]:
    result = await db.execute(
        select(Endpoint)
        .where(Endpoint.profile_id == profile_id)
        .order_by(Endpoint.position.asc(), Endpoint.id.asc())
    )
    return list(result.scalars().all())


async def _get_next_endpoint_position(db: AsyncSession, *, profile_id: int) -> int:
    result = await db.execute(
        select(func.max(Endpoint.position)).where(Endpoint.profile_id == profile_id)
    )
    max_position = result.scalar_one_or_none()
    if max_position is None:
        return 0
    return int(max_position) + 1


def _normalize_endpoint_positions(endpoints: list[Endpoint]) -> None:
    now = utc_now()
    for index, endpoint in enumerate(endpoints):
        if endpoint.position == index:
            continue
        endpoint.position = index
        endpoint.updated_at = now


@router.get("/api/endpoints", response_model=list[EndpointResponse])
async def list_endpoints(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await _list_ordered_endpoints(db, profile_id=profile_id)


@router.post("/api/endpoints", response_model=EndpointResponse, status_code=201)
async def create_endpoint(
    body: EndpointCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    endpoint_name = body.name.strip()
    if not endpoint_name:
        raise HTTPException(status_code=422, detail="name must not be empty")

    normalized_url = normalize_base_url(body.base_url)
    url_warnings = validate_base_url(normalized_url)
    if url_warnings:
        raise HTTPException(status_code=422, detail="; ".join(url_warnings))

    await _lock_profile_row(db, profile_id=profile_id)
    await _ensure_unique_endpoint_name(
        db, profile_id=profile_id, endpoint_name=endpoint_name
    )

    endpoint = Endpoint(
        profile_id=profile_id,
        name=endpoint_name,
        base_url=normalized_url,
        api_key=encrypt_secret(body.api_key),
        position=await _get_next_endpoint_position(db, profile_id=profile_id),
    )
    db.add(endpoint)
    await db.flush()
    await db.refresh(endpoint)
    return endpoint


@router.patch(
    "/api/endpoints/{endpoint_id}/position", response_model=list[EndpointResponse]
)
async def move_endpoint_position(
    endpoint_id: int,
    body: EndpointPositionMoveRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    await _lock_profile_row(db, profile_id=profile_id)
    endpoints = await _list_ordered_endpoints(db, profile_id=profile_id)
    current_index = next(
        (
            index
            for index, endpoint in enumerate(endpoints)
            if endpoint.id == endpoint_id
        ),
        None,
    )
    if current_index is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    if body.to_index >= len(endpoints):
        raise HTTPException(
            status_code=422,
            detail=f"to_index must be between 0 and {len(endpoints) - 1}",
        )

    if body.to_index == current_index:
        return endpoints

    endpoint = endpoints.pop(current_index)
    endpoints.insert(body.to_index, endpoint)
    _normalize_endpoint_positions(endpoints)
    await db.flush()
    return endpoints


@router.get("/api/endpoints/connections", response_model=ConnectionDropdownResponse)
async def list_all_connections(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    result = await db.execute(
        select(Connection)
        .where(Connection.profile_id == profile_id)
        .order_by(Connection.id.asc())
    )
    connections = list(result.scalars().all())
    return ConnectionDropdownResponse(
        items=[ConnectionDropdownItem.model_validate(item) for item in connections]
    )


@router.put("/api/endpoints/{endpoint_id}", response_model=EndpointResponse)
async def update_endpoint(
    endpoint_id: int,
    body: EndpointUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    endpoint_result = await db.execute(
        select(Endpoint).where(
            Endpoint.id == endpoint_id,
            Endpoint.profile_id == profile_id,
        )
    )
    endpoint = endpoint_result.scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    update_data = body.model_dump(exclude_unset=True)

    if "name" in update_data:
        endpoint_name = (update_data["name"] or "").strip()
        if not endpoint_name:
            raise HTTPException(status_code=422, detail="name must not be empty")
        await _ensure_unique_endpoint_name(
            db,
            profile_id=profile_id,
            endpoint_name=endpoint_name,
            exclude_id=endpoint_id,
        )
        update_data["name"] = endpoint_name

    if "base_url" in update_data:
        update_data["base_url"] = normalize_base_url(update_data["base_url"])
        url_warnings = validate_base_url(update_data["base_url"])
        if url_warnings:
            raise HTTPException(status_code=422, detail="; ".join(url_warnings))

    clear_dependent_recovery_state = False
    if "base_url" in update_data and update_data["base_url"] != endpoint.base_url:
        clear_dependent_recovery_state = True
    if "api_key" in update_data:
        incoming_api_key = update_data["api_key"]
        if incoming_api_key:
            try:
                existing_api_key = decrypt_secret(endpoint.api_key)
            except ValueError:
                existing_api_key = None
            if incoming_api_key != existing_api_key:
                clear_dependent_recovery_state = True
            update_data["api_key"] = encrypt_secret(incoming_api_key)
        else:
            update_data.pop("api_key")

    for key, value in update_data.items():
        setattr(endpoint, key, value)

    if clear_dependent_recovery_state:
        dependent_connection_ids = (
            (
                await db.execute(
                    select(Connection.id).where(
                        Connection.profile_id == profile_id,
                        Connection.endpoint_id == endpoint.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for connection_id in dependent_connection_ids:
            mark_connection_recovered(profile_id, connection_id)

    endpoint.updated_at = utc_now()
    await db.flush()
    await db.refresh(endpoint)
    return endpoint


@router.delete("/api/endpoints/{endpoint_id}")
async def delete_endpoint(
    endpoint_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    await _lock_profile_row(db, profile_id=profile_id)
    endpoint_result = await db.execute(
        select(Endpoint).where(
            Endpoint.id == endpoint_id,
            Endpoint.profile_id == profile_id,
        )
    )
    endpoint = endpoint_result.scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    in_use_rows = (
        (
            await db.execute(
                select(Connection)
                .options(selectinload(Connection.model_config_rel))
                .where(
                    Connection.endpoint_id == endpoint_id,
                    Connection.profile_id == profile_id,
                )
                .order_by(Connection.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if in_use_rows:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Cannot delete endpoint that is referenced by connections",
                "connections": [
                    {
                        "connection_id": connection.id,
                        "model_config_id": connection.model_config_id,
                        "model_id": (
                            connection.model_config_rel.model_id
                            if connection.model_config_rel is not None
                            else None
                        ),
                        "name": connection.name,
                    }
                    for connection in in_use_rows
                ],
            },
        )

    deleted_position = endpoint.position
    await db.delete(endpoint)
    await db.flush()

    remaining_endpoints = (
        (
            await db.execute(
                select(Endpoint)
                .where(
                    Endpoint.profile_id == profile_id,
                    Endpoint.position > deleted_position,
                )
                .order_by(Endpoint.position.asc(), Endpoint.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if remaining_endpoints:
        now = utc_now()
        for index, remaining_endpoint in enumerate(
            remaining_endpoints,
            start=deleted_position,
        ):
            remaining_endpoint.position = index
            remaining_endpoint.updated_at = now
        await db.flush()

    return {"deleted": True}
