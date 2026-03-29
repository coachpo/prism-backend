from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx
from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.time import ensure_utc_datetime, utc_now
from app.models.models import Connection, HeaderBlocklistRule, ModelConfig
from app.routers.connections_domains.health_check_builders import (
    _build_endpoint_ping_request,
    _build_health_check_request,
)
from app.routers.connections_domains.health_check_request_helpers import (
    _execute_health_check_request,
)
from app.services.loadbalancer.runtime_store import (
    acquire_monitoring_probe_lease,
    release_connection_lease,
)
from app.services.monitoring.routing_feedback import record_probe_outcome
from app.services.proxy_service import build_upstream_headers, build_upstream_url


@dataclass(frozen=True, slots=True)
class ProbeExecutionResult:
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
                selectinload(Connection.model_config_rel).selectinload(
                    ModelConfig.vendor
                ),
                selectinload(Connection.model_config_rel).selectinload(
                    ModelConfig.loadbalance_strategy
                ),
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


def _resolve_openai_probe_endpoint_variant(
    connection: Connection, *, api_family: str
) -> str:
    if api_family != "openai":
        return "responses"
    variant = getattr(connection, "openai_probe_endpoint_variant", "responses")
    if variant == "chat_completions":
        return "chat_completions"
    return "responses"


def _classify_probe_failure_kind(detail: str) -> str:
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


async def run_connection_probe(
    *,
    db: AsyncSession,
    client: httpx.AsyncClient,
    profile_id: int,
    connection_id: int,
    checked_at: datetime | None = None,
    acquire_probe_lease: bool = False,
    load_connection_fn=_load_connection_or_404,
    load_blocklist_rules_fn=_load_enabled_blocklist_rules,
    build_upstream_headers_fn=build_upstream_headers,
    execute_probe_request_fn=_execute_health_check_request,
    acquire_probe_lease_fn=acquire_monitoring_probe_lease,
    release_probe_lease_fn=release_connection_lease,
    record_probe_outcome_fn=record_probe_outcome,
) -> ProbeExecutionResult:
    normalized_checked_at = ensure_utc_datetime(checked_at) or utc_now()
    connection = await load_connection_fn(
        db,
        profile_id=profile_id,
        connection_id=connection_id,
    )
    endpoint = connection.endpoint_rel
    if endpoint is None:
        raise HTTPException(status_code=400, detail="Connection endpoint is missing")

    api_family = connection.model_config_rel.api_family
    model_id = connection.model_config_rel.model_id
    vendor = connection.model_config_rel.vendor
    vendor_id = getattr(vendor, "id", None)
    if not isinstance(vendor_id, int):
        raise ValueError(f"Connection {connection_id} is missing vendor metadata")

    blocklist_rules = await load_blocklist_rules_fn(db, profile_id=profile_id)
    headers = build_upstream_headers_fn(
        connection,
        api_family,
        blocklist_rules=blocklist_rules,
        endpoint=endpoint,
    )
    openai_variant = _resolve_openai_probe_endpoint_variant(
        connection,
        api_family=api_family,
    )

    lease_token: str | None = None
    if acquire_probe_lease:
        lease_result = await acquire_probe_lease_fn(
            session=db,
            profile_id=profile_id,
            connection_id=connection_id,
            lease_ttl_seconds=30,
            now_at=normalized_checked_at,
        )
        if not lease_result.admitted:
            raise HTTPException(
                status_code=409,
                detail=f"Monitoring probe unavailable: {lease_result.deny_reason}",
            )
        lease_token = lease_result.lease_token

    try:
        endpoint_ping_path, endpoint_ping_body = _build_endpoint_ping_request(
            api_family,
            model_id,
            openai_variant=openai_variant,
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
        conversation_delay_ms: int | None = None
        if endpoint_ping_status == "healthy":
            conversation_path, conversation_body = _build_health_check_request(
                api_family,
                model_id,
                openai_variant=openai_variant,
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

        fused_status = _resolve_fused_status(
            endpoint_ping_status,
            conversation_status,
        )
        detail = (
            conversation_detail
            if endpoint_ping_status == "healthy"
            else endpoint_ping_detail
        )
        failure_kind = (
            None if fused_status == "healthy" else _classify_probe_failure_kind(detail)
        )

        await record_probe_outcome_fn(
            profile_id=profile_id,
            vendor_id=vendor_id,
            model_config_id=connection.model_config_rel.id,
            connection_id=connection.id,
            endpoint_id=endpoint.id,
            endpoint_ping_status=endpoint_ping_status,
            endpoint_ping_ms=endpoint_ping_ms,
            conversation_status=conversation_status,
            conversation_delay_ms=conversation_delay_ms,
            failure_kind=failure_kind,
            detail=detail,
            checked_at=normalized_checked_at,
            session=db,
        )

        connection.health_status = (
            "healthy" if fused_status == "healthy" else "unhealthy"
        )
        connection.health_detail = detail
        connection.last_health_check = normalized_checked_at
        await db.flush()

        return ProbeExecutionResult(
            connection_id=connection.id,
            checked_at=normalized_checked_at,
            endpoint_ping_status=endpoint_ping_status,
            endpoint_ping_ms=endpoint_ping_ms,
            conversation_status=conversation_status,
            conversation_delay_ms=conversation_delay_ms,
            fused_status=fused_status,
            failure_kind=failure_kind,
            detail=detail,
        )
    finally:
        if lease_token is not None:
            await release_probe_lease_fn(
                session=db,
                profile_id=profile_id,
                lease_token=lease_token,
                now_at=normalized_checked_at,
            )


__all__ = ["ProbeExecutionResult", "run_connection_probe"]
