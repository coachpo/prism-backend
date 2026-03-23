import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi.responses import StreamingResponse

from app.services.background_tasks import background_task_manager
from app.services.costing_service import CostFieldPayload
from app.services.stats_service import extract_token_usage

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

TOKEN_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "reasoning_tokens",
)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _pick_int(*values: Any) -> int | None:
    for value in values:
        parsed = _as_int(value)
        if parsed is not None:
            return parsed
    return None


def _extract_special_usage(
    usage: dict[str, object],
) -> tuple[int | None, int | None, int | None]:
    prompt_details = usage.get("prompt_tokens_details") or usage.get(
        "input_tokens_details"
    )
    completion_details = usage.get("completion_tokens_details") or usage.get(
        "output_tokens_details"
    )

    cache_read_input_tokens = None
    cache_creation_input_tokens = None
    reasoning_tokens = None

    if isinstance(prompt_details, dict):
        cache_read_input_tokens = _pick_int(
            prompt_details.get("cached_tokens"),
            prompt_details.get("cache_read_input_tokens"),
            prompt_details.get("cached_input_tokens"),
            prompt_details.get("cachedContentTokenCount"),
        )
        cache_creation_input_tokens = _pick_int(
            prompt_details.get("cache_creation_input_tokens"),
            prompt_details.get("cache_creation_tokens"),
            prompt_details.get("cacheCreationInputTokens"),
            prompt_details.get("cacheCreationTokens"),
        )

    if isinstance(completion_details, dict):
        reasoning_tokens = _pick_int(
            completion_details.get("reasoning_tokens"),
            completion_details.get("reasoningTokenCount"),
        )

    if cache_read_input_tokens is None:
        cache_read_input_tokens = _pick_int(
            usage.get("cache_read_input_tokens"),
            usage.get("cached_input_tokens"),
            usage.get("cachedContentTokenCount"),
        )
    if cache_creation_input_tokens is None:
        cache_creation_input_tokens = _pick_int(
            usage.get("cache_creation_input_tokens"),
            usage.get("cache_creation_tokens"),
            usage.get("cacheCreationInputTokens"),
            usage.get("cacheCreationTokens"),
        )
    if reasoning_tokens is None:
        reasoning_tokens = _pick_int(
            usage.get("reasoning_tokens"),
            usage.get("reasoningTokenCount"),
        )

    return (
        cache_read_input_tokens,
        cache_creation_input_tokens,
        reasoning_tokens,
    )


def _is_sse_stream(content_type: str | None) -> bool:
    if content_type is None:
        return False
    return content_type.partition(";")[0].strip().lower() == "text/event-stream"


def _has_token_values(tokens: TokenUsage | None) -> bool:
    return bool(tokens and any(value is not None for value in tokens.values()))


@dataclass(frozen=True, slots=True)
class _SseEventTokenUpdate:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    usage_seen: bool = False


def _parse_sse_event_update(line: bytes) -> _SseEventTokenUpdate | None:
    stripped = line.strip()
    if not stripped.startswith(b"data: ") or stripped == b"data: [DONE]":
        return None

    try:
        event = json.loads(stripped[6:])
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None

    if not isinstance(event, dict):
        return None

    update = _SseEventTokenUpdate()
    usage_seen = False
    input_tokens = None
    output_tokens = None
    total_tokens = None
    cache_read_input_tokens = None
    cache_creation_input_tokens = None
    reasoning_tokens = None

    usage = event.get("usage")
    if usage is None:
        response_payload = event.get("response")
        if isinstance(response_payload, dict):
            nested_usage = response_payload.get("usage")
            if isinstance(nested_usage, dict):
                usage = nested_usage

    if isinstance(usage, dict):
        usage_seen = True
        input_tokens = _pick_int(usage.get("prompt_tokens"), usage.get("input_tokens"))
        output_tokens = _pick_int(
            usage.get("completion_tokens"),
            usage.get("output_tokens"),
        )
        total_tokens = _pick_int(usage.get("total_tokens"))
        (
            cache_read_input_tokens,
            cache_creation_input_tokens,
            reasoning_tokens,
        ) = _extract_special_usage(usage)

    if event.get("type") == "message_start":
        message = event.get("message")
        msg_usage = message.get("usage", {}) if isinstance(message, dict) else {}
        if isinstance(msg_usage, dict):
            usage_seen = True
            input_tokens = _pick_int(msg_usage.get("input_tokens"), input_tokens)
            (
                cache_read_input_tokens,
                cache_creation_input_tokens,
                reasoning_tokens,
            ) = (
                _pick_int(
                    _extract_special_usage(msg_usage)[0], cache_read_input_tokens
                ),
                _pick_int(
                    _extract_special_usage(msg_usage)[1], cache_creation_input_tokens
                ),
                _pick_int(_extract_special_usage(msg_usage)[2], reasoning_tokens),
            )

    if event.get("type") == "message_delta":
        delta_usage = event.get("usage", {})
        if isinstance(delta_usage, dict):
            usage_seen = True
            output_tokens = _pick_int(delta_usage.get("output_tokens"), output_tokens)
            (
                cache_read_input_tokens,
                cache_creation_input_tokens,
                reasoning_tokens,
            ) = (
                _pick_int(
                    _extract_special_usage(delta_usage)[0], cache_read_input_tokens
                ),
                _pick_int(
                    _extract_special_usage(delta_usage)[1], cache_creation_input_tokens
                ),
                _pick_int(_extract_special_usage(delta_usage)[2], reasoning_tokens),
            )

    gemini_usage = event.get("usageMetadata")
    if isinstance(gemini_usage, dict):
        usage_seen = True
        input_tokens = _pick_int(gemini_usage.get("promptTokenCount"), input_tokens)
        output_tokens = _pick_int(
            gemini_usage.get("candidatesTokenCount"),
            output_tokens,
        )
        total_tokens = _pick_int(gemini_usage.get("totalTokenCount"), total_tokens)
        cache_read_input_tokens = _pick_int(
            gemini_usage.get("cachedContentTokenCount"),
            cache_read_input_tokens,
        )
        reasoning_tokens = _pick_int(
            gemini_usage.get("thoughtsTokenCount"),
            reasoning_tokens,
        )

    return _SseEventTokenUpdate(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        reasoning_tokens=reasoning_tokens,
        usage_seen=usage_seen,
    )


@dataclass(slots=True)
class _StreamingFinalizationBuffer:
    keep_payload: bool
    _payload: bytearray = field(default_factory=bytearray)
    _partial_line: bytearray = field(default_factory=bytearray)
    _tokens: TokenUsage | None = None
    _usage_seen: bool = False

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return

        if self.keep_payload:
            self._payload.extend(chunk)
            return

        self._partial_line.extend(chunk)
        self._consume_complete_lines()

    def finalize(self) -> tuple[bytes | None, TokenUsage | None]:
        if self.keep_payload:
            return bytes(self._payload), None

        if self._partial_line:
            self._consume_line(bytes(self._partial_line))
            self._partial_line.clear()

        if self._tokens is None:
            return None, None

        finalized_tokens = dict(self._tokens)
        if finalized_tokens["total_tokens"] is None and (
            finalized_tokens["input_tokens"] is not None
            or finalized_tokens["output_tokens"] is not None
        ):
            finalized_tokens["total_tokens"] = (
                finalized_tokens["input_tokens"] or 0
            ) + (finalized_tokens["output_tokens"] or 0)

        if self._usage_seen:
            for field_name in (
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "reasoning_tokens",
            ):
                if finalized_tokens[field_name] is None:
                    finalized_tokens[field_name] = 0

        return None, finalized_tokens

    def _consume_complete_lines(self) -> None:
        lines = bytes(self._partial_line).split(b"\n")
        self._partial_line = bytearray(lines.pop())
        for line in lines:
            self._consume_line(line)

    def _consume_line(self, line: bytes) -> None:
        update = _parse_sse_event_update(line)
        if update is None:
            return
        if self._tokens is None:
            self._tokens = {field_name: None for field_name in TOKEN_USAGE_FIELDS}

        if update.input_tokens is not None:
            self._tokens["input_tokens"] = update.input_tokens
        if update.output_tokens is not None:
            self._tokens["output_tokens"] = update.output_tokens
        if update.total_tokens is not None:
            self._tokens["total_tokens"] = update.total_tokens
        if update.cache_read_input_tokens is not None:
            self._tokens["cache_read_input_tokens"] = update.cache_read_input_tokens
        if update.cache_creation_input_tokens is not None:
            self._tokens["cache_creation_input_tokens"] = (
                update.cache_creation_input_tokens
            )
        if update.reasoning_tokens is not None:
            self._tokens["reasoning_tokens"] = update.reasoning_tokens
        self._usage_seen = self._usage_seen or update.usage_seen


@dataclass(frozen=True, slots=True)
class StreamFinalizationSnapshot:
    audit_capture_bodies: bool
    audit_enabled: bool
    build_cost_fields: CostFieldsBuilder
    connection_id: int
    elapsed_ms: int
    endpoint_base_url: str
    endpoint_description: str | None
    endpoint_id: int | None
    log_request_fn: LogRequestFn
    model_id: str
    payload: bytes | None
    profile_id: int
    provider_id: int
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


def _build_stream_finalization_snapshot(
    *,
    deps: ProxyRuntimeDependencies,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
    response_headers: dict[str, str],
    status_code: int,
    elapsed_ms: int,
    payload: bytes | None,
    token_usage: TokenUsage | None,
) -> StreamFinalizationSnapshot:
    endpoint = target.connection.endpoint_rel
    connection = target.connection
    cost_fields_builder = state.setup.build_cost_fields

    def build_cost_fields(tokens: dict[str, int | None] | None) -> CostFieldPayload:
        return cost_fields_builder(connection, status_code, tokens)

    return StreamFinalizationSnapshot(
        audit_capture_bodies=state.setup.audit_capture_bodies,
        audit_enabled=state.setup.audit_enabled,
        build_cost_fields=build_cost_fields,
        connection_id=connection.id,
        elapsed_ms=elapsed_ms,
        endpoint_base_url=endpoint.base_url,
        endpoint_description=target.description,
        endpoint_id=connection.endpoint_id,
        log_request_fn=deps.log_request_fn,
        model_id=state.setup.model_id,
        payload=payload,
        profile_id=state.profile_id,
        provider_id=state.setup.provider_id,
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


def _extract_stream_tokens(payload: bytes) -> dict[str, int | None] | None:
    try:
        return extract_token_usage(payload)
    except Exception:
        logger.exception("Failed to extract streaming token usage")
        return None


async def _persist_stream_request_log(
    snapshot: StreamFinalizationSnapshot,
) -> int | None:
    tokens = snapshot.token_usage
    if tokens is None and snapshot.payload is not None:
        tokens = _extract_stream_tokens(snapshot.payload)
    token_values = tokens or {}
    return await snapshot.log_request_fn(
        model_id=snapshot.model_id,
        profile_id=snapshot.profile_id,
        provider_type=snapshot.provider_type,
        endpoint_id=snapshot.endpoint_id,
        connection_id=snapshot.connection_id,
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


def _enqueue_stream_finalize(
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


async def _persist_stream_request_log_inline_fallback(
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


async def build_streaming_response(
    *,
    deps: ProxyRuntimeDependencies,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
    upstream_resp: httpx.Response,
    response_headers: dict[str, str],
    elapsed_ms: int,
) -> StreamingResponse:
    async def _iter_and_log(resp: httpx.Response):
        finalization_buffer = _StreamingFinalizationBuffer(
            keep_payload=(
                not _is_sse_stream(resp.headers.get("content-type"))
                or (state.setup.audit_enabled and state.setup.audit_capture_bodies)
            )
        )
        stream_cancelled = False
        try:
            async for chunk in resp.aiter_bytes():
                if chunk:
                    finalization_buffer.append(chunk)
                    yield chunk
        except GeneratorExit:
            stream_cancelled = True
            return
        except asyncio.CancelledError:
            stream_cancelled = True
            logger.debug("Streaming response cancelled by client")
            raise
        except Exception as exc:
            logger.warning("Stream iteration failed: %s", exc)
        finally:
            try:
                await asyncio.shield(resp.aclose())
            except BaseException:
                pass

            payload, token_usage = finalization_buffer.finalize()
            snapshot = _build_stream_finalization_snapshot(
                deps=deps,
                state=state,
                target=target,
                response_headers=response_headers,
                status_code=resp.status_code,
                elapsed_ms=elapsed_ms,
                payload=payload,
                token_usage=token_usage,
            )

            request_log_ready: asyncio.Future[int | None] | None = None
            try:
                request_log_ready = _enqueue_stream_finalize(snapshot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Failed to enqueue stream finalization: profile_id=%d connection_id=%d status_code=%d",
                    snapshot.profile_id,
                    snapshot.connection_id,
                    snapshot.status_code,
                )
                try:
                    await _persist_stream_request_log_inline_fallback(snapshot)
                except asyncio.CancelledError:
                    logger.debug(
                        "Streaming request logging cancelled before completion"
                    )
                except Exception:
                    logger.exception("Failed to log streaming request")

            if request_log_ready is not None and not stream_cancelled:
                await asyncio.shield(request_log_ready)

    media_type = upstream_resp.headers.get("content-type", "text/event-stream")
    return StreamingResponse(
        _iter_and_log(upstream_resp),
        status_code=upstream_resp.status_code,
        media_type=media_type,
        headers={
            **response_headers,
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
