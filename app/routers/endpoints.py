from typing import Annotated
from datetime import datetime
import json
import time
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_db
from app.models.models import Endpoint, ModelConfig, HeaderBlocklistRule
from app.schemas.schemas import (
    EndpointCreate,
    EndpointUpdate,
    EndpointResponse,
    HealthCheckResponse,
    EndpointOwnerResponse,
)
from app.services.proxy_service import (
    build_upstream_url,
    build_upstream_headers,
    normalize_base_url,
    validate_base_url,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["endpoints"])

PRICING_FIELDS = {
    "pricing_enabled",
    "pricing_unit",
    "pricing_currency_code",
    "input_price",
    "output_price",
    "cached_input_price",
    "cache_creation_price",
    "reasoning_price",
    "missing_special_token_policy",
}


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

    normalized_url = normalize_base_url(body.base_url)
    url_warnings = validate_base_url(normalized_url)
    if url_warnings:
        raise HTTPException(status_code=422, detail="; ".join(url_warnings))

    endpoint = Endpoint(
        model_config_id=model_config_id,
        base_url=normalized_url,
        api_key=body.api_key,
        is_active=body.is_active,
        priority=body.priority,
        description=body.description,
        auth_type=body.auth_type,
        custom_headers=json.dumps(body.custom_headers) if body.custom_headers else None,
        pricing_enabled=body.pricing_enabled,
        pricing_unit=body.pricing_unit,
        pricing_currency_code=body.pricing_currency_code,
        input_price=body.input_price,
        output_price=body.output_price,
        cached_input_price=body.cached_input_price,
        cache_creation_price=body.cache_creation_price,
        reasoning_price=body.reasoning_price,
        missing_special_token_policy=body.missing_special_token_policy,
        pricing_config_version=1 if body.pricing_enabled else 0,
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
    if "base_url" in update_data:
        update_data["base_url"] = normalize_base_url(update_data["base_url"])
        url_warnings = validate_base_url(update_data["base_url"])
        if url_warnings:
            raise HTTPException(status_code=422, detail="; ".join(url_warnings))
    if "custom_headers" in update_data:
        ch = update_data["custom_headers"]
        update_data["custom_headers"] = json.dumps(ch) if ch else None

    pricing_changed = False
    for field_name in PRICING_FIELDS:
        if field_name in update_data and update_data[field_name] != getattr(
            endpoint, field_name
        ):
            pricing_changed = True
            break

    for key, value in update_data.items():
        setattr(endpoint, key, value)
    if pricing_changed:
        endpoint.pricing_config_version = (endpoint.pricing_config_version or 0) + 1
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

    effective_auth = endpoint.auth_type or provider_type
    if effective_auth == "anthropic":
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

    blocklist_rows = (
        (
            await db.execute(
                select(HeaderBlocklistRule).where(
                    HeaderBlocklistRule.enabled == True  # noqa: E712
                )
            )
        )
        .scalars()
        .all()
    )
    blocklist_rules: list[HeaderBlocklistRule] = list(blocklist_rows)
    headers = build_upstream_headers(
        endpoint, provider_type, blocklist_rules=blocklist_rules
    )

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

        # Try to extract upstream error message from response body
        upstream_msg = ""
        if resp.status_code >= 400:
            try:
                resp_json = resp.json()
                err = resp_json.get("error", {})
                if isinstance(err, dict):
                    upstream_msg = err.get("message", "")
                elif isinstance(err, str):
                    upstream_msg = err
            except Exception:
                pass

        if 200 <= resp.status_code < 300:
            health_status = "healthy"
            detail = "Connection successful"
        elif resp.status_code == 429:
            health_status = "healthy"
            detail = "Rate limited (endpoint works)"
        elif resp.status_code in (401, 403):
            health_status = "unhealthy"
            detail = f"Authentication failed (HTTP {resp.status_code})"
            if upstream_msg:
                detail += f": {upstream_msg}"
        else:
            health_status = "unhealthy"
            detail = f"HTTP {resp.status_code}"
            if upstream_msg:
                detail += f": {upstream_msg}"
    except httpx.ConnectError as e:
        detail = f"Connection failed: {e}"
    except httpx.TimeoutException:
        detail = "Connection timed out"
    except Exception as e:
        detail = f"Error: {e}"

    logger.info(
        "Health check endpoint_id=%d url=%s status=%s detail=%s",
        endpoint.id,
        upstream_url,
        health_status,
        detail,
    )

    endpoint.health_status = health_status
    endpoint.health_detail = detail
    endpoint.last_health_check = checked_at
    await db.flush()

    return HealthCheckResponse(
        endpoint_id=endpoint.id,
        health_status=health_status,
        checked_at=checked_at,
        detail=detail,
        response_time_ms=response_time_ms,
    )


@router.get(
    "/api/endpoints/{endpoint_id}/owner",
    response_model=EndpointOwnerResponse,
)
async def get_endpoint_owner(
    endpoint_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Endpoint)
        .options(selectinload(Endpoint.model_config_rel))
        .where(Endpoint.id == endpoint_id)
    )
    endpoint = result.scalar_one_or_none()
    if not endpoint:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    return EndpointOwnerResponse(
        endpoint_id=endpoint.id,
        model_config_id=endpoint.model_config_id,
        model_id=endpoint.model_config_rel.model_id,
        endpoint_description=endpoint.description,
        endpoint_base_url=endpoint.base_url,
    )
