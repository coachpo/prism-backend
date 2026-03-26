from dataclasses import dataclass
from typing import Callable, Protocol, cast

import httpx
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.models.models import Connection, HeaderBlocklistRule, ModelConfig, Provider
from app.services.costing_service import (
    CostFieldPayload,
    compute_cost_fields,
    load_costing_settings,
)
from app.services.loadbalancer.policy import (
    EffectiveLoadbalancePolicy,
    resolve_effective_loadbalance_policy,
)
from app.services.loadbalancer.planner import (
    build_attempt_plan,
    get_model_config_with_connections,
)
from app.services.loadbalancer.types import AttemptPlan
from app.services.proxy_service import (
    extract_model_from_body,
    extract_stream_flag,
    should_request_compressed_response,
)

from .proxy_request_helpers import (
    extract_model_from_path,
    get_client_headers,
    inject_openai_stream_usage_option,
    resolve_model_id,
    rewrite_model_in_body,
    rewrite_model_in_path,
    validate_provider_path_compatibility,
)


class _RequestStateWithClient(Protocol):
    http_client: httpx.AsyncClient


class _RequestAppWithClientState(Protocol):
    state: _RequestStateWithClient


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
    failover_policy: EffectiveLoadbalancePolicy
    is_streaming: bool
    method: str
    model_config: ModelConfig
    model_id: str
    provider_id: int
    provider_type: str
    probe_eligible_connection_ids: list[int]
    raw_body: bytes | None
    recovery_active: bool
    request_compressed: bool
    rewritten_body: bytes | None


async def prepare_proxy_request(
    *,
    request: Request,
    db: AsyncSession,
    raw_body: bytes | None,
    request_path: str,
    profile_id: int,
) -> ProxyRequestSetup:
    model_id = resolve_model_id(raw_body, request_path)
    if not model_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot determine model for routing. "
                "Include 'model' in the request body or use a Gemini-style model path."
            ),
        )
    model_config = await get_model_config_with_connections(db, profile_id, model_id)
    if not model_config:
        raise HTTPException(
            status_code=404, detail=f"Model '{model_id}' not configured or disabled"
        )

    provider = cast(Provider, model_config.provider)
    provider_type = provider.provider_type
    validate_provider_path_compatibility(provider_type, request_path)
    audit_enabled = provider.audit_enabled
    audit_capture_bodies = provider.audit_capture_bodies
    provider_id = provider.id
    app = cast(_RequestAppWithClientState, request.app)
    client = app.state.http_client
    is_streaming = extract_stream_flag(raw_body) if raw_body else False
    client_headers = get_client_headers(request)
    method = request.method
    upstream_model_id = model_config.model_id
    body_model_id = extract_model_from_body(raw_body) if raw_body else None
    rewritten_body = raw_body
    if raw_body and body_model_id and upstream_model_id != body_model_id:
        rewritten_body = rewrite_model_in_body(raw_body, upstream_model_id)
    if rewritten_body and is_streaming:
        rewritten_body = inject_openai_stream_usage_option(
            rewritten_body,
            provider_type,
            request_path,
        )

    path_model = extract_model_from_path(request_path)
    effective_request_path = request_path
    if path_model and upstream_model_id != path_model:
        effective_request_path = rewrite_model_in_path(
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

    attempt_plan: AttemptPlan = await build_attempt_plan(
        db,
        profile_id,
        model_config,
        utc_now(),
    )
    endpoints_to_try = attempt_plan.connections
    if not endpoints_to_try:
        raise HTTPException(
            status_code=503,
            detail=f"No active connections available for model '{model_id}'. All connections may be in cooldown.",
        )

    costing_settings = await load_costing_settings(
        db,
        profile_id=profile_id,
        model_id=model_id,
        endpoint_ids=sorted({endpoint.endpoint_id for endpoint in endpoints_to_try}),
    )

    def build_cost_fields(
        connection: Connection,
        status_code: int,
        tokens: dict[str, int | None] | None = None,
    ) -> CostFieldPayload:
        token_values = tokens or {}
        return compute_cost_fields(
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

    strategy = model_config.loadbalance_strategy
    if strategy is None:
        raise ValueError(
            f"Native model {model_config.model_id!r} is missing loadbalance_strategy"
        )

    failover_policy = resolve_effective_loadbalance_policy(strategy)

    return ProxyRequestSetup(
        audit_capture_bodies=audit_capture_bodies,
        audit_enabled=audit_enabled,
        blocklist_rules=blocklist_rules,
        build_cost_fields=build_cost_fields,
        client=client,
        client_headers=client_headers,
        effective_request_path=effective_request_path,
        endpoints_to_try=endpoints_to_try,
        failover_policy=failover_policy,
        is_streaming=is_streaming,
        method=method,
        model_config=model_config,
        model_id=model_id,
        provider_id=provider_id,
        provider_type=provider_type,
        probe_eligible_connection_ids=attempt_plan.probe_eligible_connection_ids,
        raw_body=raw_body,
        recovery_active=failover_policy.failover_recovery_enabled,
        request_compressed=request_compressed,
        rewritten_body=rewritten_body,
    )


__all__ = ["ProxyRequestSetup", "prepare_proxy_request"]
