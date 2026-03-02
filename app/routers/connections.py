from datetime import datetime

import json
import logging
import time
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_db, get_effective_profile_id
from app.models.models import Connection, Endpoint, HeaderBlocklistRule, ModelConfig
from app.schemas.schemas import (
    ConnectionCreate,
    ConnectionOwnerResponse,
    ConnectionResponse,
    ConnectionUpdate,
    HealthCheckResponse,
 )
from app.services.loadbalancer import mark_connection_recovered
from app.services.proxy_service import (
    build_upstream_headers,
    build_upstream_url,
    normalize_base_url,
    validate_base_url,
 )

logger = logging.getLogger(__name__)

router = APIRouter(tags=["connections"])

PRICING_FIELDS = {
    "pricing_enabled",
    "pricing_currency_code",
    "input_price",
    "output_price",
    "cached_input_price",
    "cache_creation_price",
    "reasoning_price",
    "missing_special_token_price_policy",
}


async def _ensure_unique_endpoint_name(
    db: AsyncSession,
    *,
    profile_id: int,
    endpoint_name: str,
    exclude_id: int | None = None,
 ) -> None:
    query = select(Endpoint).where(
        Endpoint.profile_id == profile_id,
        Endpoint.name == endpoint_name,
    )
    if exclude_id is not None:
        query = query.where(Endpoint.id != exclude_id)
    existing = (await db.execute(query)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Endpoint name '{endpoint_name}' already exists",
        )


async def _create_endpoint_from_inline(
    db: AsyncSession,
    *,
    profile_id: int,
    endpoint_name: str,
    base_url: str,
    api_key: str,
 ) -> Endpoint:
    clean_name = endpoint_name.strip()
    if not clean_name:
        raise HTTPException(
            status_code=422, detail="endpoint_create.name must not be empty"
        )

    normalized_url = normalize_base_url(base_url)
    url_warnings = validate_base_url(normalized_url)
    if url_warnings:
        raise HTTPException(status_code=422, detail="; ".join(url_warnings))

    await _ensure_unique_endpoint_name(
        db,
        profile_id=profile_id,
        endpoint_name=clean_name,
    )

    endpoint = Endpoint(
        profile_id=profile_id,
        name=clean_name,
        base_url=normalized_url,
        api_key=api_key,
    )
    db.add(endpoint)
    await db.flush()
    return endpoint


async def _load_connection_or_404(
    db: AsyncSession,
    *,
    profile_id: int,
    connection_id: int,
 ) -> Connection:
    result = await db.execute(
        select(Connection)
        .options(selectinload(Connection.endpoint_rel))
        .where(
            Connection.id == connection_id,
            Connection.profile_id == profile_id,
        )
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    return connection


@router.get(
    "/api/models/{model_config_id}/connections", response_model=list[ConnectionResponse]
 )
async def list_connections(
    model_config_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    model_result = await db.execute(
        select(ModelConfig).where(
            ModelConfig.id == model_config_id,
            ModelConfig.profile_id == profile_id,
        )
    )
    model = model_result.scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=404, detail="Model configuration not found")

    result = await db.execute(
        select(Connection)
        .options(selectinload(Connection.endpoint_rel))
        .where(
            Connection.model_config_id == model_config_id,
            Connection.profile_id == profile_id,
        )
        .order_by(Connection.priority.asc(), Connection.id.asc())
    )
    return result.scalars().all()

@router.post(
    "/api/models/{model_config_id}/connections",
    response_model=ConnectionResponse,
    status_code=201,
 )
async def create_connection(
    model_config_id: int,
    body: ConnectionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    model_result = await db.execute(
        select(ModelConfig).where(
            ModelConfig.id == model_config_id,
            ModelConfig.profile_id == profile_id,
        )
    )
    model = model_result.scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=404, detail="Model configuration not found")

    endpoint: Endpoint | None = None
    if body.endpoint_id is not None:
        endpoint_result = await db.execute(
            select(Endpoint).where(
                Endpoint.id == body.endpoint_id,
                Endpoint.profile_id == profile_id,
            )
        )
        endpoint = endpoint_result.scalar_one_or_none()
        if endpoint is None:
            raise HTTPException(status_code=404, detail="Endpoint not found")
    elif body.endpoint_create is not None:
        endpoint = await _create_endpoint_from_inline(
            db,
            profile_id=profile_id,
            endpoint_name=body.endpoint_create.name,
            base_url=body.endpoint_create.base_url,
            api_key=body.endpoint_create.api_key,
        )

    if endpoint is None:
        raise HTTPException(
            status_code=422,
            detail="Exactly one of endpoint_id or endpoint_create is required",
        )

    connection = Connection(
        profile_id=profile_id,
        model_config_id=model_config_id,
        endpoint_id=endpoint.id,
        is_active=body.is_active,
        priority=body.priority,
        name=body.name,
        auth_type=body.auth_type,
        custom_headers=json.dumps(body.custom_headers) if body.custom_headers else None,
        pricing_enabled=body.pricing_enabled,
        pricing_currency_code=body.pricing_currency_code,
        input_price=body.input_price,
        output_price=body.output_price,
        cached_input_price=body.cached_input_price,
        cache_creation_price=body.cache_creation_price,
        reasoning_price=body.reasoning_price,
        missing_special_token_price_policy=body.missing_special_token_price_policy,
        pricing_config_version=1 if body.pricing_enabled else 0,
    )
    db.add(connection)
    await db.flush()

    return await _load_connection_or_404(
        db,
        profile_id=profile_id,
        connection_id=connection.id,
    )


@router.put("/api/connections/{connection_id}", response_model=ConnectionResponse)
async def update_connection(
    connection_id: int,
    body: ConnectionUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    connection = await _load_connection_or_404(
        db,
        profile_id=profile_id,
        connection_id=connection_id,
    )
    previous_is_active = connection.is_active

    update_data = body.model_dump(exclude_unset=True)

    inline_endpoint_payload = update_data.pop("endpoint_create", None)
    if inline_endpoint_payload is not None:
        endpoint = await _create_endpoint_from_inline(
            db,
            profile_id=profile_id,
            endpoint_name=inline_endpoint_payload["name"],
            base_url=inline_endpoint_payload["base_url"],
            api_key=inline_endpoint_payload["api_key"],
        )
        update_data["endpoint_id"] = endpoint.id

    if "endpoint_id" in update_data:
        endpoint_result = await db.execute(
            select(Endpoint).where(
                Endpoint.id == update_data["endpoint_id"],
                Endpoint.profile_id == profile_id,
            )
        )
        endpoint = endpoint_result.scalar_one_or_none()
        if endpoint is None:
            raise HTTPException(status_code=404, detail="Endpoint not found")

    if "custom_headers" in update_data:
        custom_headers = update_data["custom_headers"]
        update_data["custom_headers"] = (
            json.dumps(custom_headers) if custom_headers else None
        )

    pricing_changed = any(
        field_name in update_data
        and update_data[field_name] != getattr(connection, field_name)
        for field_name in PRICING_FIELDS
    )

    for key, value in update_data.items():
        setattr(connection, key, value)

    is_active_changed = (
        "is_active" in update_data and update_data["is_active"] != previous_is_active
    )
    if is_active_changed:
        mark_connection_recovered(profile_id, connection.id)

    if pricing_changed:
        connection.pricing_config_version = (connection.pricing_config_version or 0) + 1

    connection.updated_at = datetime.utcnow()
    await db.flush()

    return await _load_connection_or_404(
        db,
        profile_id=profile_id,
        connection_id=connection.id,
    )


@router.delete("/api/connections/{connection_id}")
async def delete_connection(
    connection_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    connection_result = await db.execute(
        select(Connection).where(
            Connection.id == connection_id,
            Connection.profile_id == profile_id,
        )
    )
    connection = connection_result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    mark_connection_recovered(profile_id, connection.id)
    await db.delete(connection)
    await db.flush()
    return {"deleted": True}
@router.post(
    "/api/connections/{connection_id}/health-check",
    response_model=HealthCheckResponse,
 )
async def health_check_connection(
    connection_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    result = await db.execute(
        select(Connection)
        .options(
            selectinload(Connection.model_config_rel).selectinload(
                ModelConfig.provider
            ),
            selectinload(Connection.endpoint_rel),
        )
        .where(
            Connection.id == connection_id,
            Connection.profile_id == profile_id,
        )
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    endpoint = connection.endpoint_rel
    if endpoint is None:
        raise HTTPException(status_code=400, detail="Connection endpoint is missing")

    provider = connection.model_config_rel.provider
    provider_type = provider.provider_type
    model_id = connection.model_config_rel.model_id

    effective_auth = connection.auth_type or provider_type
    if effective_auth == "anthropic":
        request_path = "/v1/messages"
    else:
        request_path = "/v1/chat/completions"
    body = {
        "model": model_id,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }

    upstream_url = build_upstream_url(connection, request_path, endpoint=endpoint)
    blocklist_rules = list(
        (
            await db.execute(
                select(HeaderBlocklistRule).where(
                    HeaderBlocklistRule.enabled == True,  # noqa: E712
                    or_(
                        HeaderBlocklistRule.is_system == True,  # noqa: E712
                        HeaderBlocklistRule.profile_id == profile_id,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    headers = build_upstream_headers(
        connection,
        provider_type,
        blocklist_rules=blocklist_rules,
        endpoint=endpoint,
    )

    checked_at = datetime.utcnow()
    health_status = "unhealthy"
    detail = ""
    response_time_ms = 0

    client: httpx.AsyncClient = request.app.state.http_client
    try:
        start = time.monotonic()
        response = await client.post(
            upstream_url,
            headers=headers,
            json=body,
            timeout=15.0,
        )
        response_time_ms = int((time.monotonic() - start) * 1000)

        upstream_msg = ""
        if response.status_code >= 400:
            try:
                response_json = response.json()
                error = response_json.get("error", {})
                if isinstance(error, dict):
                    upstream_msg = error.get("message", "")
                elif isinstance(error, str):
                    upstream_msg = error
            except Exception:
                upstream_msg = ""

        if 200 <= response.status_code < 300:
            health_status = "healthy"
            detail = "Connection successful"
        elif response.status_code == 429:
            health_status = "healthy"
            detail = "Rate limited (connection works)"
        elif response.status_code in (401, 403):
            health_status = "unhealthy"
            detail = f"Authentication failed (HTTP {response.status_code})"
            if upstream_msg:
                detail += f": {upstream_msg}"
        else:
            health_status = "unhealthy"
            detail = f"HTTP {response.status_code}"
            if upstream_msg:
                detail += f": {upstream_msg}"
    except httpx.ConnectError as exc:
        detail = f"Connection failed: {exc}"
    except httpx.TimeoutException:
        detail = "Connection timed out"
    except Exception as exc:
        detail = f"Error: {exc}"

    logger.info(
        "Health check connection_id=%d endpoint_id=%d url=%s status=%s detail=%s",
        connection.id,
        endpoint.id,
        upstream_url,
        health_status,
        detail,
    )

    connection.health_status = health_status
    connection.health_detail = detail
    connection.last_health_check = checked_at
    await db.flush()

    return HealthCheckResponse(
        connection_id=connection.id,
        health_status=health_status,
        checked_at=checked_at,
        detail=detail,
        response_time_ms=response_time_ms,
    )

@router.get(
    "/api/connections/{connection_id}/owner",
    response_model=ConnectionOwnerResponse,
 )
async def get_connection_owner(
    connection_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    result = await db.execute(
        select(Connection)
        .options(
            selectinload(Connection.model_config_rel),
            selectinload(Connection.endpoint_rel),
        )
        .where(
            Connection.id == connection_id,
            Connection.profile_id == profile_id,
        )
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    if connection.endpoint_rel is None:
        raise HTTPException(status_code=400, detail="Connection endpoint is missing")

    return ConnectionOwnerResponse(
        connection_id=connection.id,
        model_config_id=connection.model_config_id,
        model_id=connection.model_config_rel.model_id,
        connection_name=connection.name,
        endpoint_id=connection.endpoint_rel.id,
        endpoint_name=connection.endpoint_rel.name,
        endpoint_base_url=connection.endpoint_rel.base_url,
    )
