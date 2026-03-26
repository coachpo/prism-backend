import asyncio
import json
import logging
import re
from datetime import datetime

from app.services.background_tasks import background_task_manager
from app.models.models import AuditLog, LoadbalanceEvent

logger = logging.getLogger(__name__)

AUDIT_LOG_MAX_RETRIES = 2
AUDIT_LOG_RETRY_DELAY_SECONDS = 0.25

_SENSITIVE_HEADER_PATTERN = re.compile(r"(key|secret|token|auth)", re.IGNORECASE)

_EXACT_REDACT_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "x-goog-api-key",
    }
)


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted = {}
    for name, value in headers.items():
        name_lower = name.lower()
        if name_lower == "authorization":
            redacted[name] = "Bearer [REDACTED]"
        elif name_lower in _EXACT_REDACT_HEADERS:
            redacted[name] = "[REDACTED]"
        elif _SENSITIVE_HEADER_PATTERN.search(name_lower):
            redacted[name] = "[REDACTED]"
        else:
            redacted[name] = value
    return redacted


async def _persist_audit_log(
    *,
    request_log_id: int | None,
    profile_id: int,
    vendor_id: int,
    model_id: str,
    request_method: str,
    request_url: str,
    request_headers: dict[str, str],
    request_body: bytes | None,
    response_status: int,
    response_headers: dict[str, str] | None,
    response_body: bytes | None,
    is_stream: bool,
    duration_ms: int,
    capture_bodies: bool,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
    endpoint_base_url: str | None = None,
    endpoint_description: str | None = None,
) -> None:
    from app.core.database import AsyncSessionLocal

    redacted_req_headers = redact_headers(request_headers)
    redacted_resp_headers = (
        redact_headers(response_headers) if response_headers else None
    )

    req_body_str = None
    resp_body_str = None
    if capture_bodies:
        if request_body:
            req_body_str = request_body.decode("utf-8", errors="replace")
        if response_body:
            resp_body_str = response_body.decode("utf-8", errors="replace")

    entry = AuditLog(
        request_log_id=request_log_id,
        profile_id=profile_id,
        vendor_id=vendor_id,
        model_id=model_id,
        endpoint_id=endpoint_id,
        connection_id=connection_id,
        endpoint_base_url=endpoint_base_url,
        endpoint_description=endpoint_description,
        request_method=request_method,
        request_url=request_url,
        request_headers=json.dumps(redacted_req_headers),
        request_body=req_body_str,
        response_status=response_status,
        response_headers=json.dumps(redacted_resp_headers)
        if redacted_resp_headers
        else None,
        response_body=resp_body_str,
        is_stream=is_stream,
        duration_ms=duration_ms,
    )
    async with AsyncSessionLocal() as session:
        session.add(entry)
        await session.commit()


async def record_audit_log(
    *,
    request_log_id: int | None,
    profile_id: int,
    vendor_id: int,
    model_id: str,
    request_method: str,
    request_url: str,
    request_headers: dict[str, str],
    request_body: bytes | None,
    response_status: int,
    response_headers: dict[str, str] | None,
    response_body: bytes | None,
    is_stream: bool,
    duration_ms: int,
    capture_bodies: bool,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
    endpoint_base_url: str | None = None,
    endpoint_description: str | None = None,
) -> None:
    request_headers_copy = dict(request_headers)
    response_headers_copy = dict(response_headers) if response_headers else None
    request_body_copy = (
        bytes(request_body) if capture_bodies and request_body is not None else None
    )
    response_body_copy = (
        bytes(response_body) if capture_bodies and response_body is not None else None
    )

    async def run_audit_write() -> None:
        await _persist_audit_log(
            request_log_id=request_log_id,
            profile_id=profile_id,
            vendor_id=vendor_id,
            model_id=model_id,
            request_method=request_method,
            request_url=request_url,
            request_headers=request_headers_copy,
            request_body=request_body_copy,
            response_status=response_status,
            response_headers=response_headers_copy,
            response_body=response_body_copy,
            is_stream=is_stream,
            duration_ms=duration_ms,
            capture_bodies=capture_bodies,
            endpoint_id=endpoint_id,
            connection_id=connection_id,
            endpoint_base_url=endpoint_base_url,
            endpoint_description=endpoint_description,
        )

    try:
        background_task_manager.enqueue(
            name=f"audit-log:{profile_id}:{request_log_id or 'none'}",
            run=run_audit_write,
            max_retries=AUDIT_LOG_MAX_RETRIES,
            retry_delay_seconds=AUDIT_LOG_RETRY_DELAY_SECONDS,
        )
    except Exception:
        logger.exception(
            "Failed to enqueue audit log for request_log_id=%s",
            request_log_id,
        )


async def record_loadbalance_event(
    *,
    profile_id: int,
    connection_id: int,
    event_type: str,
    failure_kind: str | None,
    consecutive_failures: int,
    cooldown_seconds: float,
    blocked_until_mono: float | None,
    model_id: str | None = None,
    endpoint_id: int | None = None,
    vendor_id: int | None = None,
    failure_threshold: int | None = None,
    backoff_multiplier: float | None = None,
    max_cooldown_seconds: int | None = None,
    max_cooldown_strikes: int | None = None,
    ban_mode: str | None = None,
    banned_until_at: datetime | None = None,
) -> None:
    """Record loadbalance event asynchronously (fire-and-forget)."""
    from app.core.database import AsyncSessionLocal

    try:
        entry = LoadbalanceEvent(
            profile_id=profile_id,
            connection_id=connection_id,
            event_type=event_type,
            failure_kind=failure_kind,
            consecutive_failures=consecutive_failures,
            cooldown_seconds=cooldown_seconds,
            blocked_until_mono=blocked_until_mono,
            model_id=model_id,
            endpoint_id=endpoint_id,
            vendor_id=vendor_id,
            failure_threshold=failure_threshold,
            backoff_multiplier=backoff_multiplier,
            max_cooldown_seconds=max_cooldown_seconds,
            max_cooldown_strikes=max_cooldown_strikes,
            ban_mode=ban_mode,
            banned_until_at=banned_until_at,
        )
        async with AsyncSessionLocal() as session:
            session.add(entry)
            await session.commit()
    except asyncio.CancelledError:
        logger.debug("Loadbalance event logging cancelled")
    except Exception:
        logger.exception("Failed to record loadbalance event")
