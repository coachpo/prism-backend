from typing import Annotated
from datetime import datetime
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_db
from app.models.models import Endpoint, ModelConfig
from app.schemas.schemas import (
    EndpointCreate,
    EndpointUpdate,
    EndpointResponse,
    HealthCheckResponse,
)
from app.services.proxy_service import build_upstream_url, build_upstream_headers

router = APIRouter(tags=["endpoints"])


@router.get(
    "/api/models/{model_config_id}/endpoints", response_model=list[EndpointResponse]
)
async def list_endpoints(
    model_config_id: int, db: Annotated[AsyncSession, Depends(get_db)]
):
    model = await db.get(ModelConfig, model_config_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model configuration not found")

    result = await db.execute(
        select(Endpoint)
        .where(Endpoint.model_config_id == model_config_id)
        .order_by(Endpoint.priority)
    )
    return result.scalars().all()


@router.post(
    "/api/models/{model_config_id}/endpoints",
    response_model=EndpointResponse,
    status_code=201,
)
async def create_endpoint(
    model_config_id: int,
    body: EndpointCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    model = await db.get(ModelConfig, model_config_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model configuration not found")

    if model.model_type == "proxy":
        raise HTTPException(
            status_code=400,
            detail="Cannot add endpoints to a proxy model",
        )

    endpoint = Endpoint(
        model_config_id=model_config_id,
        base_url=body.base_url,
        api_key=body.api_key,
        is_active=body.is_active,
        priority=body.priority,
        description=body.description,
    )
    db.add(endpoint)
    await db.flush()
    await db.refresh(endpoint)
    return endpoint


@router.put("/api/endpoints/{endpoint_id}", response_model=EndpointResponse)
async def update_endpoint(
    endpoint_id: int,
    body: EndpointUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    endpoint = await db.get(Endpoint, endpoint_id)
    if not endpoint:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(endpoint, key, value)
    endpoint.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(endpoint)
    return endpoint


@router.delete("/api/endpoints/{endpoint_id}", status_code=204)
async def delete_endpoint(
    endpoint_id: int, db: Annotated[AsyncSession, Depends(get_db)]
):
    endpoint = await db.get(Endpoint, endpoint_id)
    if not endpoint:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    await db.delete(endpoint)


@router.post(
    "/api/endpoints/{endpoint_id}/health-check",
    response_model=HealthCheckResponse,
)
async def health_check_endpoint(
    endpoint_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Endpoint)
        .options(
            selectinload(Endpoint.model_config_rel).selectinload(ModelConfig.provider)
        )
        .where(Endpoint.id == endpoint_id)
    )
    endpoint = result.scalar_one_or_none()
    if not endpoint:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    provider_type = endpoint.model_config_rel.provider.provider_type
    model_id = endpoint.model_config_rel.model_id

    # Build request path and headers using the same logic as the proxy engine
    if provider_type == "anthropic":
        request_path = "/v1/messages"
        body = {
            "model": model_id,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }
    else:
        request_path = "/v1/chat/completions"
        body = {
            "model": model_id,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }

    upstream_url = build_upstream_url(endpoint, request_path)
    headers = build_upstream_headers(endpoint, provider_type)

    checked_at = datetime.utcnow()
    health_status = "unhealthy"
    detail = ""
    response_time_ms = 0

    client: httpx.AsyncClient = request.app.state.http_client

    try:
        start = time.monotonic()
        resp = await client.post(
            upstream_url,
            headers=headers,
            json=body,
            timeout=15.0,
        )
        response_time_ms = int((time.monotonic() - start) * 1000)

        if 200 <= resp.status_code < 300:
            health_status = "healthy"
            detail = "Connection successful"
        elif resp.status_code == 429:
            health_status = "healthy"
            detail = "Rate limited (endpoint works)"
        elif resp.status_code in (401, 403):
            health_status = "unhealthy"
            detail = f"Authentication failed (HTTP {resp.status_code})"
        else:
            health_status = "unhealthy"
            detail = f"HTTP {resp.status_code}"
    except httpx.ConnectError as e:
        detail = f"Connection failed: {e}"
    except httpx.TimeoutException:
        detail = "Connection timed out"
    except Exception as e:
        detail = f"Error: {e}"

    endpoint.health_status = health_status
    endpoint.last_health_check = checked_at
    await db.flush()

    return HealthCheckResponse(
        endpoint_id=endpoint.id,
        health_status=health_status,
        checked_at=checked_at,
        detail=detail,
        response_time_ms=response_time_ms,
    )
