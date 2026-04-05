from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx
from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.time import ensure_utc_datetime, utc_now
from app.models.models import Connection, Endpoint, HeaderBlocklistRule, ModelConfig
from app.routers.connections_domains.health_check_request_helpers import (
    _execute_health_check_request,
)
from app.services.proxy_service import build_upstream_headers, build_upstream_url


@dataclass(frozen=True)
class ConnectionHealthExecutionResult:
    connection_id: int
    checked_at: datetime
    endpoint_ping_status: str
    endpoint_ping_ms: int | None
    conversation_status: str
    conversation_delay_ms: int | None
    fused_status: str
    failure_kind: str | None
    detail: str

    @property
    def health_status(self) -> str:
        return "healthy" if self.fused_status == "healthy" else "unhealthy"


@dataclass(frozen=True)
class ConnectionHealthCheckOutcome:
    endpoint_ping_status: str
    endpoint_ping_ms: int | None
    conversation_status: str
    conversation_delay_ms: int | None
    fused_status: str
    failure_kind: str | None
    detail: str
    log_url: str

    @property
    def health_status(self) -> str:
        return "healthy" if self.fused_status == "healthy" else "unhealthy"


def _build_connection_health_conversation_request(
    api_family: str,
    model_id: str,
    *,
    openai_variant: str = "responses_minimal",
) -> tuple[str, dict[str, object]]:
    if api_family == "openai":
        if openai_variant in {
            "chat_completions_minimal",
            "chat_completions_reasoning_none",
        }:
            body: dict[str, object] = {
                "model": model_id,
                "messages": [{"role": "user", "content": "."}],
                "max_tokens": 1,
            }
            if openai_variant == "chat_completions_reasoning_none":
                body["reasoning_effort"] = "none"
            return "/v1/chat/completions", body

        body: dict[str, object] = {
            "model": model_id,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "."}],
                }
            ],
            "max_output_tokens": 1,
        }
        if openai_variant == "responses_reasoning_none":
            body["reasoning"] = {"effort": "none"}
        return "/v1/responses", body
    if api_family == "anthropic":
        return "/v1/messages", {
            "model": model_id,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "."}],
        }
    if api_family == "gemini":
        return f"/v1beta/models/{model_id}:generateContent", {
            "contents": [{"role": "user", "parts": [{"text": "."}]}],
            "generationConfig": {"maxOutputTokens": 1},
        }
    raise ValueError(f"Unsupported api_family '{api_family}' for health check")


def _build_connection_health_endpoint_ping_request(
    api_family: str,
    model_id: str,
    *,
    openai_variant: str = "responses_minimal",
) -> tuple[str, dict[str, object]]:
    return _build_connection_health_conversation_request(
        api_family,
        model_id,
        openai_variant=openai_variant,
    )


async def _load_connection_or_404(
    db: AsyncSession,
    *,
    profile_id: int,
    connection_id: int,
) -> Connection:
    connection = (
        await db.execute(
            select(Connection)
            .options(
                selectinload(Connection.endpoint_rel),
                selectinload(Connection.model_config_rel),
            )
            .where(
                Connection.profile_id == profile_id,
                Connection.id == connection_id,
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    return connection


async def _load_enabled_blocklist_rules(
    db: AsyncSession,
    *,
    profile_id: int,
) -> list[HeaderBlocklistRule]:
    result = await db.execute(
        select(HeaderBlocklistRule).where(
            HeaderBlocklistRule.enabled == True,  # noqa: E712
            or_(
                HeaderBlocklistRule.is_system == True,  # noqa: E712
                HeaderBlocklistRule.profile_id == profile_id,
            ),
        )
    )
    return list(result.scalars().all())


def _resolve_openai_health_check_variant(
    connection: Connection, *, api_family: str
) -> str:
    if api_family != "openai":
        return "responses_minimal"
    variant = getattr(connection, "openai_probe_endpoint_variant", "responses_minimal")
    if variant in {
        "responses_reasoning_none",
        "chat_completions_minimal",
        "chat_completions_reasoning_none",
    }:
        return variant
    return "responses_minimal"


def _classify_health_check_failure_kind(detail: str) -> str:
    lowered = detail.lower()
    if "timed out" in lowered:
        return "timeout"
    if "connection failed" in lowered or "connect" in lowered:
        return "connect_error"
    return "transient_http"


def _resolve_fused_status(endpoint_ping_status: str, conversation_status: str) -> str:
    if endpoint_ping_status == "healthy" and conversation_status == "healthy":
        return "healthy"
    if endpoint_ping_status == "healthy" or conversation_status == "healthy":
        return "degraded"
    return "unhealthy"


async def _execute_connection_health_checks(
    *,
    client: httpx.AsyncClient,
    connection: Connection,
    endpoint: Endpoint,
    api_family: str,
    model_id: str,
    headers: dict[str, str],
    openai_variant: str = "responses_minimal",
    execute_probe_request_fn=_execute_health_check_request,
) -> ConnectionHealthCheckOutcome:
    endpoint_ping_path, endpoint_ping_body = (
        _build_connection_health_endpoint_ping_request(
            api_family,
            model_id,
            openai_variant=openai_variant,
        )
    )
    endpoint_ping_url = build_upstream_url(
        connection,
        endpoint_ping_path,
        endpoint=endpoint,
    )
    (
        endpoint_ping_status,
        endpoint_ping_detail,
        endpoint_ping_ms,
    ) = await execute_probe_request_fn(
        client,
        upstream_url=endpoint_ping_url,
        headers=headers,
        body=endpoint_ping_body,
    )

    conversation_status = endpoint_ping_status
    conversation_detail = endpoint_ping_detail
    conversation_delay_ms = endpoint_ping_ms
    log_url = endpoint_ping_url

    if endpoint_ping_status == "healthy":
        conversation_path, conversation_body = (
            _build_connection_health_conversation_request(
                api_family,
                model_id,
                openai_variant=openai_variant,
            )
        )
        conversation_url = build_upstream_url(
            connection,
            conversation_path,
            endpoint=endpoint,
        )
        (
            conversation_status,
            conversation_detail,
            conversation_delay_ms,
        ) = await execute_probe_request_fn(
            client,
            upstream_url=conversation_url,
            headers=headers,
            body=conversation_body,
        )
        log_url = conversation_url

    fused_status = _resolve_fused_status(endpoint_ping_status, conversation_status)
    detail = (
        endpoint_ping_detail
        if endpoint_ping_status != "healthy"
        else conversation_detail
    )
    failure_kind = None
    if fused_status != "healthy":
        failure_kind = _classify_health_check_failure_kind(detail)

    return ConnectionHealthCheckOutcome(
        endpoint_ping_status=endpoint_ping_status,
        endpoint_ping_ms=endpoint_ping_ms,
        conversation_status=conversation_status,
        conversation_delay_ms=conversation_delay_ms,
        fused_status=fused_status,
        failure_kind=failure_kind,
        detail=detail,
        log_url=log_url,
    )


async def probe_connection_health(
    *,
    db: AsyncSession,
    client: httpx.AsyncClient,
    profile_id: int,
    connection: Connection,
    endpoint: Endpoint,
    api_family: str,
    model_id: str,
    openai_variant: str = "responses_minimal",
    load_blocklist_rules_fn=_load_enabled_blocklist_rules,
    build_upstream_headers_fn=build_upstream_headers,
    execute_probe_request_fn=_execute_health_check_request,
) -> tuple[str, str, int, str]:
    blocklist_rules = await load_blocklist_rules_fn(db, profile_id=profile_id)
    headers = build_upstream_headers_fn(
        connection,
        api_family,
        blocklist_rules=blocklist_rules,
        endpoint=endpoint,
    )
    result = await _execute_connection_health_checks(
        client=client,
        connection=connection,
        endpoint=endpoint,
        api_family=api_family,
        model_id=model_id,
        headers=headers,
        openai_variant=openai_variant,
        execute_probe_request_fn=execute_probe_request_fn,
    )
    return (
        result.health_status,
        result.detail,
        result.conversation_delay_ms or result.endpoint_ping_ms or 0,
        result.log_url,
    )


async def run_connection_health_check(
    *,
    db: AsyncSession,
    client: httpx.AsyncClient,
    profile_id: int,
    connection_id: int,
    checked_at: datetime | None = None,
    load_connection_fn=_load_connection_or_404,
    load_blocklist_rules_fn=_load_enabled_blocklist_rules,
    build_upstream_headers_fn=build_upstream_headers,
    execute_probe_request_fn=_execute_health_check_request,
) -> ConnectionHealthExecutionResult:
    normalized_checked_at = ensure_utc_datetime(checked_at) or utc_now()
    connection = await load_connection_fn(
        db,
        profile_id=profile_id,
        connection_id=connection_id,
    )
    endpoint = connection.endpoint_rel
    if endpoint is None:
        raise HTTPException(status_code=400, detail="Connection endpoint is missing")

    model_config = connection.model_config_rel
    if model_config is None:
        raise HTTPException(status_code=400, detail="Connection model is missing")

    api_family = model_config.api_family
    model_id = model_config.model_id
    blocklist_rules = await load_blocklist_rules_fn(db, profile_id=profile_id)
    headers = build_upstream_headers_fn(
        connection,
        api_family,
        blocklist_rules=blocklist_rules,
        endpoint=endpoint,
    )
    openai_variant = _resolve_openai_health_check_variant(
        connection,
        api_family=api_family,
    )
    result = await _execute_connection_health_checks(
        client=client,
        connection=connection,
        endpoint=endpoint,
        api_family=api_family,
        model_id=model_id,
        headers=headers,
        openai_variant=openai_variant,
        execute_probe_request_fn=execute_probe_request_fn,
    )

    connection.health_status = (
        "healthy" if result.fused_status == "healthy" else "unhealthy"
    )
    connection.health_detail = result.detail
    connection.last_health_check = normalized_checked_at
    await db.flush()

    return ConnectionHealthExecutionResult(
        connection_id=connection.id,
        checked_at=normalized_checked_at,
        endpoint_ping_status=result.endpoint_ping_status,
        endpoint_ping_ms=result.endpoint_ping_ms,
        conversation_status=result.conversation_status,
        conversation_delay_ms=result.conversation_delay_ms,
        fused_status=result.fused_status,
        failure_kind=result.failure_kind,
        detail=result.detail,
    )


__all__ = [
    "ConnectionHealthExecutionResult",
    "_build_connection_health_conversation_request",
    "_build_connection_health_endpoint_ping_request",
    "_execute_connection_health_checks",
    "probe_connection_health",
    "run_connection_health_check",
]
