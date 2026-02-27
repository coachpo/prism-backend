from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_db
from app.models.models import Connection, Endpoint
from app.schemas.schemas import (
    ConnectionDropdownResponse,
    EndpointCreate,
    EndpointResponse,
    EndpointUpdate,
)
from app.services.proxy_service import normalize_base_url, validate_base_url

router = APIRouter(tags=["endpoints"])


async def _ensure_unique_endpoint_name(
    db: AsyncSession,
    *,
    endpoint_name: str,
    exclude_id: int | None = None,
) -> None:
    query = select(Endpoint).where(Endpoint.name == endpoint_name)
    if exclude_id is not None:
        query = query.where(Endpoint.id != exclude_id)
    existing = (await db.execute(query)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Endpoint name '{endpoint_name}' already exists",
        )


@router.get("/api/endpoints", response_model=list[EndpointResponse])
async def list_endpoints(db: Annotated[AsyncSession, Depends(get_db)]):
    result = await db.execute(select(Endpoint).order_by(Endpoint.id.asc()))
    return result.scalars().all()


@router.post("/api/endpoints", response_model=EndpointResponse, status_code=201)
async def create_endpoint(
    body: EndpointCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    endpoint_name = body.name.strip()
    if not endpoint_name:
        raise HTTPException(status_code=422, detail="name must not be empty")

    normalized_url = normalize_base_url(body.base_url)
    url_warnings = validate_base_url(normalized_url)
    if url_warnings:
        raise HTTPException(status_code=422, detail="; ".join(url_warnings))

    await _ensure_unique_endpoint_name(db, endpoint_name=endpoint_name)

    endpoint = Endpoint(
        name=endpoint_name,
        base_url=normalized_url,
        api_key=body.api_key,
    )
    db.add(endpoint)
    await db.flush()
    await db.refresh(endpoint)
    return endpoint



@router.get("/api/endpoints/connections", response_model=ConnectionDropdownResponse)
async def list_all_connections(db: Annotated[AsyncSession, Depends(get_db)]):
    result = await db.execute(select(Connection).order_by(Connection.id.asc()))
    connections = result.scalars().all()
    return ConnectionDropdownResponse(items=connections)

@router.put("/api/endpoints/{endpoint_id}", response_model=EndpointResponse)
async def update_endpoint(
    endpoint_id: int,
    body: EndpointUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    endpoint = await db.get(Endpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    update_data = body.model_dump(exclude_unset=True)

    if "name" in update_data:
        endpoint_name = (update_data["name"] or "").strip()
        if not endpoint_name:
            raise HTTPException(status_code=422, detail="name must not be empty")
        await _ensure_unique_endpoint_name(
            db,
            endpoint_name=endpoint_name,
            exclude_id=endpoint_id,
        )
        update_data["name"] = endpoint_name

    if "base_url" in update_data:
        update_data["base_url"] = normalize_base_url(update_data["base_url"])
        url_warnings = validate_base_url(update_data["base_url"])
        if url_warnings:
            raise HTTPException(status_code=422, detail="; ".join(url_warnings))

    for key, value in update_data.items():
        setattr(endpoint, key, value)

    endpoint.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(endpoint)
    return endpoint


@router.delete("/api/endpoints/{endpoint_id}", status_code=204)
async def delete_endpoint(
    endpoint_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    endpoint = await db.get(Endpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    in_use_rows = (
        (
            await db.execute(
                select(Connection)
                .options(selectinload(Connection.model_config_rel))
                .where(Connection.endpoint_id == endpoint_id)
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
                        "description": connection.description,
                    }
                    for connection in in_use_rows
                ],
            },
        )

    await db.delete(endpoint)
