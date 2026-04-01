from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime

import httpx
from fastapi import HTTPException  # pyright: ignore[reportMissingImports]
from sqlalchemy import or_, select  # pyright: ignore[reportMissingImports]
from sqlalchemy.ext.asyncio import AsyncSession  # pyright: ignore[reportMissingImports]
from sqlalchemy.orm import selectinload  # pyright: ignore[reportMissingImports]

from app.core.database import AsyncSessionLocal
from app.core.time import ensure_utc_datetime, utc_now
from app.models.models import (
    Connection,
    HeaderBlocklistRule,
    ModelConfig,
    RoutingConnectionRuntimeState,
)
from app.services.background_tasks import BackgroundTaskManager
from app.routers.connections_domains.health_check_request_helpers import (
    _execute_health_check_request,
)
from app.services.loadbalancer.runtime_store import (
    acquire_monitoring_probe_lease,
    release_connection_lease,
)
from app.services.monitoring.routing_feedback import record_probe_outcome
from app.services.proxy_service import build_upstream_headers, build_upstream_url


MIN_MONITORING_PROBE_JITTER_SECONDS = 0.0
MAX_MONITORING_PROBE_JITTER_SECONDS = 10.0
DEFAULT_MONITORING_PROBE_INTERVAL_SECONDS = 300
MIN_MONITORING_PROBE_INTERVAL_SECONDS = 30
MAX_MONITORING_PROBE_INTERVAL_SECONDS = 3_600

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
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


@dataclass(frozen=True)
class ProbeCheckOutcome:
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


def resolve_monitoring_probe_interval_seconds(value: int | None) -> int:
    if value is None:
        return DEFAULT_MONITORING_PROBE_INTERVAL_SECONDS
    return max(
        MIN_MONITORING_PROBE_INTERVAL_SECONDS,
        min(MAX_MONITORING_PROBE_INTERVAL_SECONDS, int(value)),
    )


def is_connection_due_for_probe(
    state_row: RoutingConnectionRuntimeState | None,
    *,
    interval_seconds: int,
    now_at: datetime,
) -> bool:
    if state_row is None:
        return True

    if state_row.circuit_state == "open":
        probe_available_at = ensure_utc_datetime(state_row.probe_available_at)
        blocked_until_at = ensure_utc_datetime(state_row.blocked_until_at)
        if probe_available_at is None:
            probe_available_at = blocked_until_at
        return probe_available_at is not None and probe_available_at <= now_at

    if state_row.circuit_state == "half_open":
        return False

    last_probe_at = ensure_utc_datetime(state_row.last_probe_at)
    if last_probe_at is None:
        return True
    return last_probe_at.timestamp() + interval_seconds <= now_at.timestamp()


def _build_monitoring_conversation_request(
    api_family: str,
    model_id: str,
    *,
    openai_variant: str = "responses",
) -> tuple[str, dict[str, object]]:
    if api_family == "openai":
        if openai_variant == "chat_completions":
            return "/v1/chat/completions", {
                "model": model_id,
                "messages": [{"role": "user", "content": "."}],
                "max_tokens": 1,
                "reasoning_effort": "none",
            }

        return "/v1/responses", {
            "model": model_id,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": ".",
                        }
                    ],
                }
            ],
            "max_output_tokens": 1,
            "reasoning": {"effort": "none"},
            "store": False,
            "stream": True,
        }
    if api_family == "anthropic":
        return "/v1/messages", {
            "model": model_id,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "."}],
        }
    if api_family == "gemini":
        return f"/v1beta/models/{model_id}:generateContent", {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": "."}],
                }
            ],
            "generationConfig": {"maxOutputTokens": 1},
        }
    raise ValueError(f"Unsupported api_family '{api_family}' for health check")


def _build_monitoring_endpoint_ping_request(
    api_family: str,
    model_id: str,
    *,
    openai_variant: str = "responses",
) -> tuple[str, dict[str, object]]:
    if api_family == "openai" and openai_variant == "responses":
        return _build_monitoring_conversation_request(
            api_family,
            model_id,
            openai_variant="responses",
        )
    return _build_monitoring_conversation_request(
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


async def _load_runtime_state(
    db: AsyncSession,
    *,
    profile_id: int,
    connection_id: int,
) -> RoutingConnectionRuntimeState | None:
    return (
        await db.execute(
            select(RoutingConnectionRuntimeState).where(
                RoutingConnectionRuntimeState.profile_id == profile_id,
                RoutingConnectionRuntimeState.connection_id == connection_id,
            )
        )
    ).scalar_one_or_none()


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


def _resolve_monitoring_probe_jitter_seconds() -> float:
    return random.uniform(
        MIN_MONITORING_PROBE_JITTER_SECONDS,
        MAX_MONITORING_PROBE_JITTER_SECONDS,
    )


def _resolve_enqueue_probe_resources(
    *,
    background_task_manager: BackgroundTaskManager | None,
    client: httpx.AsyncClient | None,
) -> tuple[BackgroundTaskManager | None, httpx.AsyncClient | None]:
    if background_task_manager is not None and client is not None:
        return background_task_manager, client

    try:
        from app.main import app as prism_app
    except Exception:
        return background_task_manager, client

    resolved_background_task_manager = background_task_manager or getattr(
        prism_app.state,
        "background_task_manager",
        None,
    )
    resolved_client = client or getattr(prism_app.state, "http_client", None)
    return resolved_background_task_manager, resolved_client


def enqueue_connection_probe(
    *,
    profile_id: int,
    connection_id: int,
    background_task_manager: BackgroundTaskManager | None = None,
    client: httpx.AsyncClient | None = None,
) -> bool:
    resolved_background_task_manager, resolved_client = (
        _resolve_enqueue_probe_resources(
            background_task_manager=background_task_manager,
            client=client,
        )
    )
    if (
        resolved_background_task_manager is None
        or not getattr(resolved_background_task_manager, "started", False)
        or resolved_client is None
    ):
        return False

    async def run_immediate_probe() -> None:
        async with AsyncSessionLocal() as session:
            try:
                await run_connection_probe(
                    db=session,
                    client=resolved_client,
                    profile_id=profile_id,
                    connection_id=connection_id,
                    acquire_probe_lease=True,
                    resolve_probe_jitter_seconds_fn=lambda: 0.0,
                )
                await session.commit()
            except HTTPException as exc:
                await session.rollback()
                if exc.status_code == 409:
                    logger.debug(
                        "Skipping immediate probe enqueue run: profile_id=%d connection_id=%d detail=%s",
                        profile_id,
                        connection_id,
                        exc.detail,
                    )
                    return
                raise
            except Exception:
                await session.rollback()
                raise

    try:
        resolved_background_task_manager.enqueue(
            name=f"monitoring-immediate-probe:{profile_id}:{connection_id}",
            run=run_immediate_probe,
            max_retries=0,
        )
        return True
    except Exception:
        logger.exception(
            "Failed to enqueue immediate connection probe: profile_id=%d connection_id=%d",
            profile_id,
            connection_id,
        )
        return False


async def _execute_monitoring_probe_checks(
    *,
    client: httpx.AsyncClient,
    connection: Connection,
    endpoint,
    api_family: str,
    model_id: str,
    headers: dict[str, str],
    openai_variant: str = "responses",
    execute_probe_request_fn=_execute_health_check_request,
) -> ProbeCheckOutcome:
    endpoint_ping_path, endpoint_ping_body = _build_monitoring_endpoint_ping_request(
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
    log_url = endpoint_ping_url
    if endpoint_ping_status == "healthy":
        conversation_path, conversation_body = _build_monitoring_conversation_request(
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
        log_url = conversation_url

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
    return ProbeCheckOutcome(
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
    endpoint,
    api_family: str,
    model_id: str,
    openai_variant: str = "responses",
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
    result = await _execute_monitoring_probe_checks(
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
    resolve_probe_jitter_seconds_fn=_resolve_monitoring_probe_jitter_seconds,
    sleep_fn=asyncio.sleep,
) -> ProbeExecutionResult:
    normalized_checked_at = ensure_utc_datetime(checked_at) or utc_now()
    if acquire_probe_lease:
        jitter_seconds = min(
            MAX_MONITORING_PROBE_JITTER_SECONDS,
            max(
                MIN_MONITORING_PROBE_JITTER_SECONDS,
                float(resolve_probe_jitter_seconds_fn()),
            ),
        )
        if jitter_seconds > 0.0:
            await sleep_fn(jitter_seconds)
        normalized_checked_at = utc_now()

    connection = await load_connection_fn(
        db,
        profile_id=profile_id,
        connection_id=connection_id,
    )
    if acquire_probe_lease:
        state_row = await _load_runtime_state(
            db,
            profile_id=profile_id,
            connection_id=connection_id,
        )
        interval_seconds = resolve_monitoring_probe_interval_seconds(
            getattr(connection, "monitoring_probe_interval_seconds", None)
        )
        if not is_connection_due_for_probe(
            state_row,
            interval_seconds=interval_seconds,
            now_at=normalized_checked_at,
        ):
            raise HTTPException(
                status_code=409,
                detail="Monitoring probe unavailable: probe_not_due",
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
            interval_seconds=interval_seconds,
            now_at=normalized_checked_at,
        )
        if not lease_result.admitted:
            raise HTTPException(
                status_code=409,
                detail=f"Monitoring probe unavailable: {lease_result.deny_reason}",
            )
        lease_token = lease_result.lease_token

    probe_failed = False
    try:
        result = await _execute_monitoring_probe_checks(
            client=client,
            connection=connection,
            endpoint=endpoint,
            api_family=api_family,
            model_id=model_id,
            headers=headers,
            openai_variant=openai_variant,
            execute_probe_request_fn=execute_probe_request_fn,
        )

        await record_probe_outcome_fn(
            profile_id=profile_id,
            vendor_id=vendor_id,
            model_config_id=connection.model_config_rel.id,
            connection_id=connection.id,
            endpoint_id=endpoint.id,
            endpoint_ping_status=result.endpoint_ping_status,
            endpoint_ping_ms=result.endpoint_ping_ms,
            conversation_status=result.conversation_status,
            conversation_delay_ms=result.conversation_delay_ms,
            failure_kind=result.failure_kind,
            detail=result.detail,
            checked_at=normalized_checked_at,
            session=db,
        )

        connection.health_status = (
            "healthy" if result.fused_status == "healthy" else "unhealthy"
        )
        connection.health_detail = result.detail
        connection.last_health_check = normalized_checked_at
        await db.flush()

        return ProbeExecutionResult(
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
    except BaseException:
        probe_failed = True
        raise
    finally:
        if lease_token is not None:
            if probe_failed:
                try:
                    async with AsyncSessionLocal() as cleanup_session:
                        await release_probe_lease_fn(
                            session=cleanup_session,
                            profile_id=profile_id,
                            lease_token=lease_token,
                            now_at=normalized_checked_at,
                        )
                        await cleanup_session.commit()
                except Exception:
                    logger.exception(
                        "Monitoring probe lease cleanup failed: profile_id=%d connection_id=%d",
                        profile_id,
                        connection_id,
                    )
            else:
                await release_probe_lease_fn(
                    session=db,
                    profile_id=profile_id,
                    lease_token=lease_token,
                    now_at=normalized_checked_at,
                )


__all__ = [
    "DEFAULT_MONITORING_PROBE_INTERVAL_SECONDS",
    "ProbeExecutionResult",
    "enqueue_connection_probe",
    "is_connection_due_for_probe",
    "probe_connection_health",
    "resolve_monitoring_probe_interval_seconds",
    "run_connection_probe",
]
