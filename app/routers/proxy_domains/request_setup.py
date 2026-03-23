import inspect
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.models.models import Connection, HeaderBlocklistRule, ModelConfig
from app.services.costing_service import CostFieldPayload
from app.services.proxy_service import (
    extract_model_from_body,
    extract_stream_flag,
    should_request_compressed_response,
)

from .proxy_request_helpers import (
    _extract_model_from_path,
    _get_client_headers,
    _resolve_model_id,
    _rewrite_model_in_body,
    _rewrite_model_in_path,
    _validate_provider_path_compatibility,
)


@dataclass(slots=True)
class ProxyRequestSetup:
    audit_capture_bodies: bool
    audit_enabled: bool
    blocklist_rules: list[HeaderBlocklistRule]
    build_cost_fields: Callable[..., CostFieldPayload]
    client: httpx.AsyncClient
    client_headers: dict[str, str]
    effective_request_path: str
    endpoints_to_try: list[Connection]
    is_streaming: bool
    method: str
    model_config: ModelConfig
    model_id: str
    provider_id: int
    provider_type: str
    raw_body: bytes | None
    recovery_active: bool
    request_compressed: bool
    rewritten_body: bytes | None


async def prepare_proxy_request(
    *,
    build_attempt_plan_fn: Callable[
        [AsyncSession, int, ModelConfig, datetime | None],
        Awaitable[list[Connection]] | list[Connection],
    ],
    compute_cost_fields_fn: Callable[..., CostFieldPayload],
    get_model_config_with_connections_fn: Callable[
        [AsyncSession, int, str],
        Awaitable[ModelConfig | None],
    ],
    load_costing_settings_fn: Callable[..., Awaitable[Any]],
    request: Request,
    db: AsyncSession,
    raw_body: bytes | None,
    request_path: str,
    profile_id: int,
) -> ProxyRequestSetup:
    model_id = _resolve_model_id(raw_body, request_path)
    if not model_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot determine model for routing. "
                "Include 'model' in the request body or use a Gemini-style model path."
            ),
        )
    model_config = await get_model_config_with_connections_fn(db, profile_id, model_id)
    if not model_config:
        raise HTTPException(
            status_code=404, detail=f"Model '{model_id}' not configured or disabled"
        )

    provider_type = model_config.provider.provider_type
    _validate_provider_path_compatibility(provider_type, request_path)
    audit_enabled = model_config.provider.audit_enabled
    audit_capture_bodies = model_config.provider.audit_capture_bodies
    provider_id = model_config.provider.id
    client: httpx.AsyncClient = request.app.state.http_client
    is_streaming = extract_stream_flag(raw_body) if raw_body else False
    client_headers = _get_client_headers(request)
    method = request.method
    upstream_model_id = model_config.model_id
    body_model_id = extract_model_from_body(raw_body) if raw_body else None
    rewritten_body = raw_body
    if raw_body and body_model_id and upstream_model_id != body_model_id:
        rewritten_body = _rewrite_model_in_body(raw_body, upstream_model_id)

    path_model = _extract_model_from_path(request_path)
    effective_request_path = request_path
    if path_model and upstream_model_id != path_model:
        effective_request_path = _rewrite_model_in_path(
            request_path, path_model, upstream_model_id
        )

    request_compressed = should_request_compressed_response(
        audit_enabled, audit_capture_bodies
    )
    blocklist_rules = list(
        (
            (
                await db.execute(
                    select(HeaderBlocklistRule).where(
                        HeaderBlocklistRule.enabled == True,  # noqa: E712
                        (HeaderBlocklistRule.is_system == True)  # noqa: E712
                        | (HeaderBlocklistRule.profile_id == profile_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    )

    attempt_plan_result = build_attempt_plan_fn(
        db,
        profile_id,
        model_config,
        utc_now(),
    )
    endpoints_to_try = (
        await attempt_plan_result
        if inspect.isawaitable(attempt_plan_result)
        else attempt_plan_result
    )
    if not endpoints_to_try:
        raise HTTPException(
            status_code=503,
            detail=f"No active connections available for model '{model_id}'. All connections may be in cooldown.",
        )

    costing_settings = await load_costing_settings_fn(
        db,
        profile_id=profile_id,
        model_id=model_id,
        endpoint_ids=sorted(
            {
                endpoint.endpoint_id
                for endpoint in endpoints_to_try
                if endpoint.endpoint_id is not None
            }
        ),
    )

    def build_cost_fields(
        connection,
        status_code: int,
        tokens: dict[str, int | None] | None = None,
    ) -> CostFieldPayload:
        token_values = tokens or {}
        return compute_cost_fields_fn(
            connection=connection,
            pricing_template=connection.pricing_template_rel,
            endpoint=connection.endpoint_rel,
            model_id=model_id,
            status_code=status_code,
            input_tokens=token_values.get("input_tokens"),
            output_tokens=token_values.get("output_tokens"),
            cache_read_input_tokens=token_values.get("cache_read_input_tokens"),
            cache_creation_input_tokens=token_values.get("cache_creation_input_tokens"),
            reasoning_tokens=token_values.get("reasoning_tokens"),
            settings=costing_settings,
        )

    recovery_active = (
        model_config.lb_strategy == "failover"
        and model_config.failover_recovery_enabled
    )

    return ProxyRequestSetup(
        audit_capture_bodies=audit_capture_bodies,
        audit_enabled=audit_enabled,
        blocklist_rules=blocklist_rules,
        build_cost_fields=build_cost_fields,
        client=client,
        client_headers=client_headers,
        effective_request_path=effective_request_path,
        endpoints_to_try=endpoints_to_try,
        is_streaming=is_streaming,
        method=method,
        model_config=model_config,
        model_id=model_id,
        provider_id=provider_id,
        provider_type=provider_type,
        raw_body=raw_body,
        recovery_active=recovery_active,
        request_compressed=request_compressed,
        rewritten_body=rewritten_body,
    )


__all__ = ["ProxyRequestSetup", "prepare_proxy_request"]
