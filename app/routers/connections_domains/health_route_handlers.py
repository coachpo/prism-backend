from collections.abc import Awaitable, Callable

import httpx
from fastapi import Request  # pyright: ignore[reportMissingImports]
from sqlalchemy.ext.asyncio import AsyncSession  # pyright: ignore[reportMissingImports]

from app.core.time import utc_now
from app.models.models import Connection, Endpoint, ModelConfig
from app.schemas.schemas import (
    ConnectionCreate,
    ConnectionHealthCheckPreviewResponse,
    HealthCheckResponse,
)
from app.services.monitoring.probe_runner import (
    ProbeExecutionResult,
    probe_connection_health,
)

from .connection_crud_helpers import _load_model_or_404, _serialize_custom_headers
from .crud_handlers.shared import resolve_preview_endpoint


def _build_preview_connection(
    *,
    profile_id: int,
    model_config: ModelConfig,
    endpoint: Endpoint,
    body: ConnectionCreate,
) -> Connection:
    api_family = getattr(model_config, "api_family", None)
    endpoint_id = getattr(endpoint, "id", None)
    resolved_endpoint_id = endpoint_id if isinstance(endpoint_id, int) else 0
    return Connection(
        profile_id=profile_id,
        model_config_id=model_config.id,
        endpoint_id=resolved_endpoint_id,
        is_active=body.is_active,
        priority=0,
        name=body.name,
        auth_type=body.auth_type,
        custom_headers=_serialize_custom_headers(body.custom_headers),
        pricing_template_id=body.pricing_template_id,
        qps_limit=body.qps_limit,
        max_in_flight_non_stream=body.max_in_flight_non_stream,
        max_in_flight_stream=body.max_in_flight_stream,
        monitoring_probe_interval_seconds=body.monitoring_probe_interval_seconds,
        openai_probe_endpoint_variant=(
            body.openai_probe_endpoint_variant
            if api_family == "openai"
            else "responses_minimal"
        ),
    )


async def perform_connection_health_check(
    *,
    connection_id: int,
    request: Request,
    db: AsyncSession,
    profile_id: int,
    run_connection_probe_fn: Callable[..., Awaitable[ProbeExecutionResult]],
) -> HealthCheckResponse:
    client: httpx.AsyncClient = request.app.state.http_client
    result = await run_connection_probe_fn(
        db=db,
        client=client,
        profile_id=profile_id,
        connection_id=connection_id,
    )
    return HealthCheckResponse(
        connection_id=result.connection_id,
        health_status=result.health_status,
        checked_at=result.checked_at,
        detail=result.detail,
        response_time_ms=result.conversation_delay_ms or result.endpoint_ping_ms or 0,
    )


async def perform_connection_health_check_preview(
    *,
    model_config_id: int,
    body: ConnectionCreate,
    request: Request,
    db: AsyncSession,
    profile_id: int,
    load_model_fn: Callable[..., Awaitable[ModelConfig]] = _load_model_or_404,
    load_preview_endpoint_fn: Callable[
        ..., Awaitable[Endpoint]
    ] = resolve_preview_endpoint,
    probe_connection_health_fn: Callable[..., Awaitable[tuple[str, str, int, str]]] = (
        probe_connection_health
    ),
) -> ConnectionHealthCheckPreviewResponse:
    client: httpx.AsyncClient = request.app.state.http_client
    model_config = await load_model_fn(
        db,
        profile_id=profile_id,
        model_config_id=model_config_id,
    )
    endpoint = await load_preview_endpoint_fn(
        body=body,
        db=db,
        profile_id=profile_id,
    )
    preview_connection = _build_preview_connection(
        profile_id=profile_id,
        model_config=model_config,
        endpoint=endpoint,
        body=body,
    )
    health_status, detail, response_time_ms, _ = await probe_connection_health_fn(
        db=db,
        client=client,
        profile_id=profile_id,
        connection=preview_connection,
        endpoint=endpoint,
        api_family=model_config.api_family,
        model_id=model_config.model_id,
        openai_variant=preview_connection.openai_probe_endpoint_variant,
    )
    return ConnectionHealthCheckPreviewResponse(
        health_status=health_status,
        checked_at=utc_now(),
        detail=detail,
        response_time_ms=response_time_ms,
    )


__all__ = [
    "perform_connection_health_check",
    "perform_connection_health_check_preview",
]
