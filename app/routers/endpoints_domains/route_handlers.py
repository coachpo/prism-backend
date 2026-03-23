from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.time import utc_now
from app.models.models import Connection, Endpoint
from app.schemas.schemas import (
    ConnectionDropdownItem,
    ConnectionDropdownResponse,
    EndpointCreate,
    EndpointPositionMoveRequest,
    EndpointResponse,
    EndpointUpdate,
)
from app.services.loadbalancer import clear_current_state
from app.services.proxy_service import normalize_base_url, validate_base_url

from .helpers import (
    build_duplicate_endpoint_name,
    ensure_unique_endpoint_name,
    get_next_endpoint_position,
    list_dependent_connection_ids,
    list_endpoint_usage_rows,
    list_ordered_endpoints,
    load_endpoint_or_404,
    lock_profile_row,
    normalize_endpoint_positions,
    renumber_endpoints_after_delete,
)


async def list_endpoints_for_profile(
    db: AsyncSession,
    *,
    profile_id: int,
) -> list[Endpoint]:
    return await list_ordered_endpoints(db, profile_id=profile_id)


async def create_endpoint_record(
    body: EndpointCreate,
    db: AsyncSession,
    *,
    profile_id: int,
) -> Endpoint:
    endpoint_name = body.name.strip()
    if not endpoint_name:
        raise HTTPException(status_code=422, detail="name must not be empty")

    normalized_url = normalize_base_url(body.base_url)
    url_warnings = validate_base_url(normalized_url)
    if url_warnings:
        raise HTTPException(status_code=422, detail="; ".join(url_warnings))

    await lock_profile_row(db, profile_id=profile_id)
    await ensure_unique_endpoint_name(
        db,
        profile_id=profile_id,
        endpoint_name=endpoint_name,
    )

    endpoint = Endpoint(
        profile_id=profile_id,
        name=endpoint_name,
        base_url=normalized_url,
        api_key=encrypt_secret(body.api_key),
        position=await get_next_endpoint_position(db, profile_id=profile_id),
    )
    db.add(endpoint)
    await db.flush()
    await db.refresh(endpoint)
    return endpoint


async def move_endpoint_position_record(
    endpoint_id: int,
    body: EndpointPositionMoveRequest,
    db: AsyncSession,
    *,
    profile_id: int,
) -> list[Endpoint]:
    await lock_profile_row(db, profile_id=profile_id)
    endpoints = await list_ordered_endpoints(db, profile_id=profile_id)
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
    normalize_endpoint_positions(endpoints)
    await db.flush()
    return endpoints


async def list_connection_dropdown_response(
    db: AsyncSession,
    *,
    profile_id: int,
) -> ConnectionDropdownResponse:
    result = await db.execute(
        select(Connection)
        .where(Connection.profile_id == profile_id)
        .order_by(Connection.id.asc())
    )
    connections = list(result.scalars().all())
    return ConnectionDropdownResponse(
        items=[ConnectionDropdownItem.model_validate(item) for item in connections]
    )


async def update_endpoint_record(
    endpoint_id: int,
    body: EndpointUpdate,
    db: AsyncSession,
    *,
    profile_id: int,
) -> Endpoint:
    endpoint = await load_endpoint_or_404(
        db,
        endpoint_id=endpoint_id,
        profile_id=profile_id,
    )
    update_data = body.model_dump(exclude_unset=True)

    if "name" in update_data:
        endpoint_name = (update_data["name"] or "").strip()
        if not endpoint_name:
            raise HTTPException(status_code=422, detail="name must not be empty")
        await ensure_unique_endpoint_name(
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
        dependent_connection_ids = await list_dependent_connection_ids(
            db,
            profile_id=profile_id,
            endpoint_id=endpoint.id,
        )
        for connection_id in dependent_connection_ids:
            await clear_current_state(profile_id, connection_id)

    endpoint.updated_at = utc_now()
    await db.flush()
    await db.refresh(endpoint)
    return endpoint


async def duplicate_endpoint_record(
    endpoint_id: int,
    db: AsyncSession,
    *,
    profile_id: int,
) -> Endpoint:
    await lock_profile_row(db, profile_id=profile_id)
    source_endpoint = await load_endpoint_or_404(
        db,
        endpoint_id=endpoint_id,
        profile_id=profile_id,
    )

    existing_names_result = await db.execute(
        select(Endpoint.name).where(Endpoint.profile_id == profile_id)
    )
    duplicate_name = build_duplicate_endpoint_name(
        source_endpoint.name,
        set(existing_names_result.scalars().all()),
    )

    duplicate = Endpoint(
        profile_id=profile_id,
        name=duplicate_name,
        base_url=source_endpoint.base_url,
        api_key=source_endpoint.api_key,
        position=await get_next_endpoint_position(db, profile_id=profile_id),
    )
    db.add(duplicate)
    await db.flush()
    await db.refresh(duplicate)
    return duplicate


async def delete_endpoint_record(
    endpoint_id: int,
    db: AsyncSession,
    *,
    profile_id: int,
) -> dict[str, bool]:
    await lock_profile_row(db, profile_id=profile_id)
    endpoint = await load_endpoint_or_404(
        db,
        endpoint_id=endpoint_id,
        profile_id=profile_id,
    )

    in_use_rows = await list_endpoint_usage_rows(
        db,
        profile_id=profile_id,
        endpoint_id=endpoint_id,
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
    await renumber_endpoints_after_delete(
        db,
        profile_id=profile_id,
        deleted_position=deleted_position,
    )
    return {"deleted": True}


__all__ = [
    "create_endpoint_record",
    "delete_endpoint_record",
    "duplicate_endpoint_record",
    "list_connection_dropdown_response",
    "list_endpoints_for_profile",
    "move_endpoint_position_record",
    "update_endpoint_record",
]
