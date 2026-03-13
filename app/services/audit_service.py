import asyncio
import json
import logging
import re

from app.models.models import AuditLog, LoadbalanceEvent
from app.schemas.domains.stats import LoadbalanceEventListItem
from app.services.realtime import connection_manager

logger = logging.getLogger(__name__)

BODY_MAX_BYTES = 64 * 1024
TRUNCATION_MARKER = "[TRUNCATED]"

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


def _truncate_body(body: str | None) -> str | None:
    if body is None:
        return None
    if len(body.encode("utf-8", errors="replace")) > BODY_MAX_BYTES:
        truncated = body.encode("utf-8", errors="replace")[:BODY_MAX_BYTES].decode(
            "utf-8", errors="replace"
        )
        return truncated + TRUNCATION_MARKER
    return body


async def record_audit_log(
    *,
    request_log_id: int | None,
    profile_id: int,
    provider_id: int,
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

    try:
        redacted_req_headers = redact_headers(request_headers)
        redacted_resp_headers = (
            redact_headers(response_headers) if response_headers else None
        )

        req_body_str = None
        resp_body_str = None
        if capture_bodies:
            if request_body:
                req_body_str = _truncate_body(
                    request_body.decode("utf-8", errors="replace")
                )
            if response_body:
                resp_body_str = _truncate_body(
                    response_body.decode("utf-8", errors="replace")
                )

        entry = AuditLog(
            request_log_id=request_log_id,
            profile_id=profile_id,
            provider_id=provider_id,
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
            try:
                await session.refresh(entry)
                if request_log_id is not None:
                    await connection_manager.broadcast_to_profile(
                        profile_id=profile_id,
                        channel="request_logs",
                        message={
                            "type": "request_logs.audit_ready",
                            "request_log_id": request_log_id,
                            "audit_log_id": entry.id,
                        },
                    )
            except Exception:
                logger.exception(
                    "Failed to broadcast audit-ready payload; falling back to dirty signal"
                )
                try:
                    await connection_manager.broadcast_to_profile(
                        profile_id=profile_id,
                        channel="request_logs",
                        message={"type": "request_logs.dirty"},
                    )
                except Exception:
                    logger.debug(
                        "Failed to broadcast dirty fallback for audit log (non-critical)"
                    )
    except asyncio.CancelledError:
        logger.debug("Audit logging cancelled")
    except Exception:
        logger.exception("Failed to record audit log")


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
    provider_id: int | None = None,
    failure_threshold: int | None = None,
    backoff_multiplier: float | None = None,
    max_cooldown_seconds: int | None = None,
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
            provider_id=provider_id,
            failure_threshold=failure_threshold,
            backoff_multiplier=backoff_multiplier,
            max_cooldown_seconds=max_cooldown_seconds,
        )
        async with AsyncSessionLocal() as session:
            session.add(entry)
            await session.commit()
            try:
                await session.refresh(entry)
                serialized_event = LoadbalanceEventListItem.model_validate(
                    entry
                ).model_dump(mode="json")
                await connection_manager.broadcast_to_profile(
                    profile_id=profile_id,
                    channel="loadbalance_events",
                    message={
                        "type": "loadbalance_events.new",
                        "event": serialized_event,
                    },
                )
            except Exception:
                logger.exception(
                    "Failed to broadcast loadbalance payload; falling back to dirty signal"
                )
                try:
                    await connection_manager.broadcast_to_profile(
                        profile_id=profile_id,
                        channel="loadbalance_events",
                        message={"type": "loadbalance_events.dirty"},
                    )
                except Exception:
                    logger.debug(
                        "Failed to broadcast dirty fallback for loadbalance event (non-critical)"
                    )
    except asyncio.CancelledError:
        logger.debug("Loadbalance event logging cancelled")
    except Exception:
        logger.exception("Failed to record loadbalance event")
