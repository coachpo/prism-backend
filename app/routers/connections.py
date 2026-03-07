# ruff: noqa: F401
import json
import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.time import utc_now
from app.dependencies import get_db, get_effective_profile_id
from app.models.models import (
    Connection,
    Endpoint,
    HeaderBlocklistRule,
    ModelConfig,
    PricingTemplate,
)
from app.schemas.schemas import (
    ConnectionCreate,
    ConnectionOwnerResponse,
    ConnectionResponse,
    ConnectionUpdate,
    ConnectionPricingTemplateUpdate,
    HealthCheckResponse,
)
from app.services.loadbalancer import mark_connection_recovered
from app.services.proxy_service import (
    build_upstream_headers,
    build_upstream_url,
    normalize_base_url,
    validate_base_url,
)
from app.routers.connections_domains.health_check_request_helpers import (
    _execute_health_check_request,
    _extract_upstream_error_message,
    _map_health_check_response,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["connections"])


def _build_health_check_request(
    provider_type: str, model_id: str
) -> tuple[str, dict[str, object]]:
    if provider_type == "openai":
        return "/v1/responses", {
            "model": model_id,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hi"}],
                }
            ],
            "max_output_tokens": 1,
        }
    if provider_type == "anthropic":
        return "/v1/messages", {
            "model": model_id,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }
    if provider_type == "gemini":
        return f"/v1beta/models/{model_id}:generateContent", {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": "hi"}],
                }
            ],
            "generationConfig": {"maxOutputTokens": 1},
        }
    raise ValueError(f"Unsupported provider type '{provider_type}' for health check")


def _build_openai_legacy_health_check_request(
    model_id: str,
) -> tuple[str, dict[str, object]]:
    return "/v1/chat/completions", {
        "model": model_id,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }


def _build_openai_responses_basic_health_check_request(
    model_id: str,
) -> tuple[str, dict[str, object]]:
    return "/v1/responses", {
        "model": model_id,
        "input": "hi",
    }


async def _probe_connection_health(
    *,
    client: httpx.AsyncClient,
    connection: Connection,
    endpoint: Endpoint,
    provider_type: str,
    model_id: str,
    headers: dict[str, str],
) -> tuple[str, str, int, str]:
    request_path, body = _build_health_check_request(provider_type, model_id)
    upstream_url = build_upstream_url(connection, request_path, endpoint=endpoint)
    health_status, detail, response_time_ms = await _execute_health_check_request(
        client,
        upstream_url=upstream_url,
        headers=headers,
        body=body,
    )
    log_url = upstream_url

    if provider_type == "openai" and health_status != "healthy":
        responses_basic_path, responses_basic_body = (
            _build_openai_responses_basic_health_check_request(model_id)
        )
        responses_basic_url = build_upstream_url(
            connection, responses_basic_path, endpoint=endpoint
        )
        (
            responses_basic_status,
            responses_basic_detail,
            responses_basic_response_time_ms,
        ) = await _execute_health_check_request(
            client,
            upstream_url=responses_basic_url,
            headers=headers,
            body=responses_basic_body,
        )
        if responses_basic_status == "healthy":
            return (
                "healthy",
                f"{responses_basic_detail} (fallback /v1/responses basic input)",
                responses_basic_response_time_ms,
                responses_basic_url,
            )

        fallback_path, fallback_body = _build_openai_legacy_health_check_request(
            model_id
        )
        fallback_url = build_upstream_url(connection, fallback_path, endpoint=endpoint)
        (
            fallback_status,
            fallback_detail,
            fallback_response_time_ms,
        ) = await _execute_health_check_request(
            client,
            upstream_url=fallback_url,
            headers=headers,
            body=fallback_body,
        )
        if fallback_status == "healthy":
            return (
                "healthy",
                f"{fallback_detail} (legacy fallback /v1/chat/completions)",
                fallback_response_time_ms,
                fallback_url,
            )
        detail_parts = [
            detail,
            f"fallback /v1/responses basic input failed: {responses_basic_detail}",
            f"fallback /v1/chat/completions failed: {fallback_detail}",
        ]
        detail = "; ".join(part for part in detail_parts if part)
        response_time_ms = (
            fallback_response_time_ms
            or responses_basic_response_time_ms
            or response_time_ms
        )
        log_url = f"{upstream_url} -> {responses_basic_url} -> {fallback_url}"

    return health_status, detail, response_time_ms, log_url


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

async def _get_next_endpoint_position(db: AsyncSession, *, profile_id: int) -> int:
    result = await db.execute(
        select(func.max(Endpoint.position)).where(Endpoint.profile_id == profile_id)
    )
    max_position = result.scalar_one_or_none()
    if max_position is None:
        return 0
    return int(max_position) + 1



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
        position=await _get_next_endpoint_position(db, profile_id=profile_id),
    )
    db.add(endpoint)
    await db.flush()
    return endpoint


async def _validate_pricing_template_id(
    db: AsyncSession,
    *,
    profile_id: int,
    pricing_template_id: int | None,
    allow_null: bool = True,
) -> int | None:
    if pricing_template_id is None:
        if allow_null:
            return None
        raise HTTPException(status_code=422, detail="pricing_template_id is required")

    template_result = await db.execute(
        select(PricingTemplate).where(
            PricingTemplate.id == pricing_template_id,
            PricingTemplate.profile_id == profile_id,
        )
    )
    template = template_result.scalar_one_or_none()
    if template is None:
        raise HTTPException(status_code=404, detail="Pricing template not found")
    return template.id


async def _load_connection_or_404(
    db: AsyncSession,
    *,
    profile_id: int,
    connection_id: int,
) -> Connection:
    result = await db.execute(
        select(Connection)
        .options(
            selectinload(Connection.endpoint_rel),
            selectinload(Connection.pricing_template_rel),
        )
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
        .options(
            selectinload(Connection.endpoint_rel),
            selectinload(Connection.pricing_template_rel),
        )
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

    pricing_template_id = await _validate_pricing_template_id(
        db,
        profile_id=profile_id,
        pricing_template_id=body.pricing_template_id,
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
        pricing_template_id=pricing_template_id,
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
    previous_endpoint_id = connection.endpoint_id
    previous_auth_type = connection.auth_type
    previous_custom_headers = connection.custom_headers
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

    if "pricing_template_id" in update_data:
        update_data["pricing_template_id"] = await _validate_pricing_template_id(
            db,
            profile_id=profile_id,
            pricing_template_id=update_data["pricing_template_id"],
        )
    if "custom_headers" in update_data:
        custom_headers = update_data["custom_headers"]
        update_data["custom_headers"] = (
            json.dumps(custom_headers) if custom_headers else None
        )

    for key, value in update_data.items():
        setattr(connection, key, value)

    clear_recovery_state = False
    if "is_active" in update_data and update_data["is_active"] != previous_is_active:
        clear_recovery_state = True
    if (
        "endpoint_id" in update_data
        and update_data["endpoint_id"] != previous_endpoint_id
    ):
        clear_recovery_state = True
    if "auth_type" in update_data and update_data["auth_type"] != previous_auth_type:
        clear_recovery_state = True
    if (
        "custom_headers" in update_data
        and update_data["custom_headers"] != previous_custom_headers
    ):
        clear_recovery_state = True
    if clear_recovery_state:
        mark_connection_recovered(profile_id, connection.id)
    connection.updated_at = utc_now()
    await db.flush()

    return await _load_connection_or_404(
        db,
        profile_id=profile_id,
        connection_id=connection.id,
    )


@router.put(
    "/api/connections/{connection_id}/pricing-template",
    response_model=ConnectionResponse,
)
async def set_connection_pricing_template(
    connection_id: int,
    body: ConnectionPricingTemplateUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    connection = await _load_connection_or_404(
        db,
        profile_id=profile_id,
        connection_id=connection_id,
    )
    connection.pricing_template_id = await _validate_pricing_template_id(
        db,
        profile_id=profile_id,
        pricing_template_id=body.pricing_template_id,
    )
    connection.updated_at = utc_now()
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

    checked_at = utc_now()

    client: httpx.AsyncClient = request.app.state.http_client
    health_status, detail, response_time_ms, log_url = await _probe_connection_health(
        client=client,
        connection=connection,
        endpoint=endpoint,
        provider_type=provider_type,
        model_id=model_id,
        headers=headers,
    )

    logger.info(
        "Health check connection_id=%d endpoint_id=%d url=%s status=%s detail=%s",
        connection.id,
        endpoint.id,
        log_url,
        health_status,
        detail,
    )

    connection.health_status = health_status
    connection.health_detail = detail
    connection.last_health_check = checked_at
    if health_status == "healthy":
        mark_connection_recovered(profile_id, connection.id)

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
