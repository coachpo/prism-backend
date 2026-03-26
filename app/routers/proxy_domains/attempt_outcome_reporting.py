import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.services.background_tasks import background_task_manager
from app.services.costing_service import CostFieldPayload

from .attempt_types import (
    ProxyAttemptTarget,
    ProxyRequestState,
    ProxyRuntimeDependencies,
)

logger = logging.getLogger(__name__)

LogRequestFn = Callable[..., Awaitable[int | None]]
RecordAuditLogFn = Callable[..., Awaitable[None]]
CostFieldsBuilder = Callable[[dict[str, int | None] | None], CostFieldPayload]
TokenUsage = dict[str, int | None]


def _lower_header_map(headers: dict[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {key.lower(): value for key, value in headers.items()}


def _parse_json_object(raw_body: bytes | None) -> dict[str, object] | None:
    if not raw_body:
        return None
    try:
        parsed = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_gemini_response_id_from_sse(raw_body: bytes | None) -> str | None:
    if not raw_body:
        return None
    for line in raw_body.splitlines():
        stripped = line.strip()
        if not stripped.startswith(b"data: ") or stripped == b"data: [DONE]":
            continue
        payload = _parse_json_object(stripped[6:])
        if payload is None:
            continue
        response_id = payload.get("responseId")
        if isinstance(response_id, str) and response_id:
            return response_id
        nested_response = payload.get("response")
        if isinstance(nested_response, dict):
            nested_response_id = nested_response.get("responseId")
            if isinstance(nested_response_id, str) and nested_response_id:
                return nested_response_id
    return None


def extract_provider_correlation_id(
    *,
    provider_type: str,
    response_headers: dict[str, str] | None,
    response_body: bytes | None,
    request_headers: dict[str, str] | None,
) -> str | None:
    normalized_response_headers = _lower_header_map(response_headers)
    normalized_request_headers = _lower_header_map(request_headers)

    if provider_type == "openai":
        return normalized_response_headers.get(
            "x-request-id"
        ) or normalized_request_headers.get("x-client-request-id")

    if provider_type == "anthropic":
        header_request_id = normalized_response_headers.get("request-id")
        if header_request_id:
            return header_request_id
        payload = _parse_json_object(response_body)
        body_request_id = payload.get("request_id") if payload is not None else None
        return body_request_id if isinstance(body_request_id, str) else None

    if provider_type == "gemini":
        payload = _parse_json_object(response_body)
        if payload is not None:
            response_id = payload.get("responseId")
            if isinstance(response_id, str) and response_id:
                return response_id
        return _extract_gemini_response_id_from_sse(response_body)

    return None


def response_error_detail(raw_body: bytes | None) -> str | None:
    if raw_body is None:
        return None
    return raw_body.decode("utf-8", errors="replace")[:500]


async def record_request_log(
    *,
    deps: ProxyRuntimeDependencies,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
    status_code: int,
    response_headers: dict[str, str] | None,
    response_body: bytes | None,
    elapsed_ms: int,
    is_stream: bool,
    error_detail: str | None = None,
    tokens: dict[str, int | None] | None = None,
) -> int | None:
    token_values = tokens or {}
    endpoint = target.connection.endpoint_rel
    return await deps.log_request_fn(
        model_id=state.setup.model_id,
        profile_id=state.profile_id,
        provider_type=state.setup.provider_type,
        endpoint_id=target.connection.endpoint_id,
        connection_id=target.connection.id,
        ingress_request_id=state.setup.ingress_request_id,
        attempt_number=target.attempt_number,
        provider_correlation_id=extract_provider_correlation_id(
            provider_type=state.setup.provider_type,
            response_headers=response_headers,
            response_body=response_body,
            request_headers=target.headers,
        ),
        endpoint_base_url=endpoint.base_url,
        endpoint_description=target.description,
        status_code=status_code,
        response_time_ms=elapsed_ms,
        is_stream=is_stream,
        request_path=state.request_path,
        error_detail=error_detail,
        input_tokens=token_values.get("input_tokens"),
        output_tokens=token_values.get("output_tokens"),
        total_tokens=token_values.get("total_tokens"),
        **state.setup.build_cost_fields(target.connection, status_code, tokens),
    )


async def record_attempt_audit(
    *,
    deps: ProxyRuntimeDependencies,
    request_log_id: int | None,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
    status_code: int,
    response_headers: dict[str, str] | None,
    response_body: bytes | None,
    is_stream: bool,
    elapsed_ms: int,
) -> None:
    if not state.setup.audit_enabled:
        return
    if request_log_id is None:
        return

    endpoint = target.connection.endpoint_rel
    await deps.record_audit_log_fn(
        request_log_id=request_log_id,
        profile_id=state.profile_id,
        provider_id=state.setup.provider_id,
        endpoint_id=target.connection.endpoint_id,
        connection_id=target.connection.id,
        endpoint_base_url=endpoint.base_url,
        endpoint_description=target.description,
        model_id=state.setup.model_id,
        request_method=state.setup.method,
        request_url=target.upstream_url,
        request_headers=target.headers,
        request_body=target.endpoint_body,
        response_status=status_code,
        response_headers=response_headers,
        response_body=response_body,
        is_stream=is_stream,
        duration_ms=elapsed_ms,
        capture_bodies=state.setup.audit_capture_bodies,
    )


async def log_and_audit_attempt(
    *,
    deps: ProxyRuntimeDependencies,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
    status_code: int,
    response_headers: dict[str, str] | None,
    response_body: bytes | None,
    is_stream: bool,
    elapsed_ms: int,
    error_detail: str | None = None,
    tokens: dict[str, int | None] | None = None,
) -> int | None:
    request_log_id = await record_request_log(
        deps=deps,
        state=state,
        target=target,
        status_code=status_code,
        response_headers=response_headers,
        response_body=response_body,
        elapsed_ms=elapsed_ms,
        is_stream=is_stream,
        error_detail=error_detail,
        tokens=tokens,
    )
    await record_attempt_audit(
        deps=deps,
        request_log_id=request_log_id,
        state=state,
        target=target,
        status_code=status_code,
        response_headers=response_headers,
        response_body=response_body,
        is_stream=is_stream,
        elapsed_ms=elapsed_ms,
    )
    return request_log_id


@dataclass(frozen=True, slots=True)
class StreamFinalizationSnapshot:
    attempt_number: int
    audit_capture_bodies: bool
    audit_enabled: bool
    build_cost_fields: CostFieldsBuilder
    connection_id: int
    elapsed_ms: int
    endpoint_base_url: str
    endpoint_description: str | None
    endpoint_id: int | None
    ingress_request_id: str
    log_request_fn: LogRequestFn
    model_id: str
    payload: bytes | None
    profile_id: int
    provider_id: int
    provider_correlation_id: str | None
    provider_type: str
    record_audit_log_fn: RecordAuditLogFn
    request_body: bytes | None
    request_headers: dict[str, str]
    request_method: str
    request_path: str
    response_headers: dict[str, str]
    status_code: int
    token_usage: TokenUsage | None
    upstream_url: str


def build_stream_finalization_snapshot(
    *,
    deps: ProxyRuntimeDependencies,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
    response_headers: dict[str, str],
    status_code: int,
    elapsed_ms: int,
    payload: bytes | None,
    provider_correlation_id: str | None,
    token_usage: TokenUsage | None,
) -> StreamFinalizationSnapshot:
    endpoint = target.connection.endpoint_rel
    connection = target.connection
    cost_fields_builder = state.setup.build_cost_fields

    def build_cost_fields(tokens: dict[str, int | None] | None) -> CostFieldPayload:
        return cost_fields_builder(connection, status_code, tokens)

    return StreamFinalizationSnapshot(
        audit_capture_bodies=state.setup.audit_capture_bodies,
        attempt_number=target.attempt_number,
        audit_enabled=state.setup.audit_enabled,
        build_cost_fields=build_cost_fields,
        connection_id=connection.id,
        elapsed_ms=elapsed_ms,
        endpoint_base_url=endpoint.base_url,
        endpoint_description=target.description,
        endpoint_id=connection.endpoint_id,
        ingress_request_id=state.setup.ingress_request_id,
        log_request_fn=deps.log_request_fn,
        model_id=state.setup.model_id,
        payload=payload,
        profile_id=state.profile_id,
        provider_id=state.setup.provider_id,
        provider_correlation_id=provider_correlation_id,
        provider_type=state.setup.provider_type,
        record_audit_log_fn=deps.record_audit_log_fn,
        request_body=bytes(target.endpoint_body)
        if target.endpoint_body is not None
        else None,
        request_headers=dict(target.headers),
        request_method=state.setup.method,
        request_path=state.request_path,
        response_headers=dict(response_headers),
        status_code=status_code,
        token_usage=token_usage,
        upstream_url=target.upstream_url,
    )


async def _persist_stream_request_log(
    snapshot: StreamFinalizationSnapshot,
) -> int | None:
    tokens = snapshot.token_usage
    token_values = tokens or {}
    return await snapshot.log_request_fn(
        model_id=snapshot.model_id,
        profile_id=snapshot.profile_id,
        provider_type=snapshot.provider_type,
        endpoint_id=snapshot.endpoint_id,
        connection_id=snapshot.connection_id,
        ingress_request_id=snapshot.ingress_request_id,
        attempt_number=snapshot.attempt_number,
        provider_correlation_id=snapshot.provider_correlation_id
        or extract_provider_correlation_id(
            provider_type=snapshot.provider_type,
            response_headers=snapshot.response_headers,
            response_body=snapshot.payload,
            request_headers=snapshot.request_headers,
        ),
        endpoint_base_url=snapshot.endpoint_base_url,
        endpoint_description=snapshot.endpoint_description,
        status_code=snapshot.status_code,
        response_time_ms=snapshot.elapsed_ms,
        is_stream=True,
        request_path=snapshot.request_path,
        error_detail=None,
        input_tokens=token_values.get("input_tokens"),
        output_tokens=token_values.get("output_tokens"),
        total_tokens=token_values.get("total_tokens"),
        **snapshot.build_cost_fields(tokens),
    )


async def _queue_stream_audit_follow_up(
    snapshot: StreamFinalizationSnapshot,
    *,
    request_log_id: int | None,
) -> None:
    if not snapshot.audit_enabled or request_log_id is None:
        return

    await snapshot.record_audit_log_fn(
        request_log_id=request_log_id,
        profile_id=snapshot.profile_id,
        provider_id=snapshot.provider_id,
        endpoint_id=snapshot.endpoint_id,
        connection_id=snapshot.connection_id,
        endpoint_base_url=snapshot.endpoint_base_url,
        endpoint_description=snapshot.endpoint_description,
        model_id=snapshot.model_id,
        request_method=snapshot.request_method,
        request_url=snapshot.upstream_url,
        request_headers=snapshot.request_headers,
        request_body=snapshot.request_body,
        response_status=snapshot.status_code,
        response_headers=snapshot.response_headers,
        response_body=snapshot.payload if snapshot.audit_capture_bodies else None,
        is_stream=True,
        duration_ms=snapshot.elapsed_ms,
        capture_bodies=snapshot.audit_capture_bodies,
    )


def enqueue_stream_finalize(
    snapshot: StreamFinalizationSnapshot,
) -> asyncio.Future[int | None]:
    loop = asyncio.get_running_loop()
    request_log_ready: asyncio.Future[int | None] = loop.create_future()

    async def run_stream_finalize() -> None:
        request_log_id: int | None = None
        try:
            request_log_id = await _persist_stream_request_log(snapshot)
        except asyncio.CancelledError:
            logger.debug("Streaming request logging cancelled before completion")
            raise
        except Exception:
            logger.exception("Failed to log streaming request")
        finally:
            if not request_log_ready.done():
                request_log_ready.set_result(request_log_id)

        try:
            await _queue_stream_audit_follow_up(
                snapshot,
                request_log_id=request_log_id,
            )
        except asyncio.CancelledError:
            logger.debug("Streaming audit follow-up cancelled before completion")
            raise
        except Exception:
            logger.exception("Failed to queue streaming audit follow-up")

    background_task_manager.enqueue(
        name=(
            "stream-finalize:"
            f"{snapshot.profile_id}:"
            f"{snapshot.connection_id}:"
            f"{snapshot.status_code}"
        ),
        run=run_stream_finalize,
    )
    return request_log_ready


async def persist_stream_request_log_inline_fallback(
    snapshot: StreamFinalizationSnapshot,
) -> int | None:
    try:
        request_log_task = asyncio.create_task(
            _persist_stream_request_log(snapshot),
            name="proxy-stream-request-log-fallback",
        )
    except RuntimeError:
        logger.debug(
            "Event loop closed before inline streaming request logging fallback could be scheduled"
        )
        return None

    try:
        return await asyncio.shield(request_log_task)
    except asyncio.CancelledError:
        return await request_log_task


__all__ = [
    "build_stream_finalization_snapshot",
    "enqueue_stream_finalize",
    "extract_provider_correlation_id",
    "log_and_audit_attempt",
    "persist_stream_request_log_inline_fallback",
    "record_attempt_audit",
    "response_error_detail",
]
