import json
import logging
import re

from app.models.models import AuditLog

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
            if response_body and not is_stream:
                resp_body_str = _truncate_body(
                    response_body.decode("utf-8", errors="replace")
                )

        entry = AuditLog(
            request_log_id=request_log_id,
            provider_id=provider_id,
            model_id=model_id,
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
    except Exception:
        logger.exception("Failed to record audit log")
