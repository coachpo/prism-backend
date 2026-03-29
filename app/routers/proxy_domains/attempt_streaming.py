import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import cast

import httpx
from fastapi.responses import StreamingResponse

from app.services.stats_service import extract_token_usage

from .attempt_outcome_reporting import (
    build_stream_finalization_snapshot,
    enqueue_stream_finalize,
    persist_stream_request_log_inline_fallback,
)
from .attempt_types import (
    PreparedExecutionResponse,
    ProxyAttemptTarget,
    ProxyRequestState,
    ProxyRuntimeDependencies,
)
from .proxy_request_helpers import classify_failover_failure, is_recovery_success_status

logger = logging.getLogger(__name__)
TokenUsage = dict[str, int | None]

TOKEN_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "reasoning_tokens",
)

STREAMING_FINALIZATION_BUFFER_MAX_BYTES = 64 * 1024
TRUNCATED_SSE_SENTINEL = b"...[TRUNCATED]..."
TRUNCATED_SSE_PREFIX_BYTES = 256


def _as_int(value: object) -> int | None:
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


def _pick_int(*values: object) -> int | None:
    for value in values:
        parsed = _as_int(value)
        if parsed is not None:
            return parsed
    return None


def _as_object_dict(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
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
    prompt_details_dict = _as_object_dict(prompt_details)
    completion_details_dict = _as_object_dict(completion_details)

    cache_read_input_tokens = None
    cache_creation_input_tokens = None
    reasoning_tokens = None

    if prompt_details_dict is not None:
        cache_read_input_tokens = _pick_int(
            prompt_details_dict.get("cached_tokens"),
            prompt_details_dict.get("cache_read_input_tokens"),
            prompt_details_dict.get("cached_input_tokens"),
            prompt_details_dict.get("cachedContentTokenCount"),
        )
        cache_creation_input_tokens = _pick_int(
            prompt_details_dict.get("cache_creation_input_tokens"),
            prompt_details_dict.get("cache_creation_tokens"),
            prompt_details_dict.get("cacheCreationInputTokens"),
            prompt_details_dict.get("cacheCreationTokens"),
        )

    if completion_details_dict is not None:
        reasoning_tokens = _pick_int(
            completion_details_dict.get("reasoning_tokens"),
            completion_details_dict.get("reasoningTokenCount"),
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


def _extract_json_object_after_key(line: bytes, key: bytes) -> dict[str, object] | None:
    key_index = line.rfind(key)
    if key_index == -1:
        return None

    colon_index = line.find(b":", key_index + len(key))
    if colon_index == -1:
        return None

    object_start = line.find(b"{", colon_index + 1)
    if object_start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False

    for index in range(object_start, len(line)):
        byte = line[index]

        if in_string:
            if escape_next:
                escape_next = False
            elif byte == 0x5C:
                escape_next = True
            elif byte == 0x22:
                in_string = False
            continue

        if byte == 0x22:
            in_string = True
            continue
        if byte == 0x7B:
            depth += 1
            continue
        if byte == 0x7D:
            depth -= 1
            if depth == 0:
                try:
                    parsed = cast(object, json.loads(line[object_start : index + 1]))
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    return None
                return _as_object_dict(parsed)

    return None


def _is_sse_stream(content_type: str | None) -> bool:
    if content_type is None:
        return False
    return content_type.partition(";")[0].strip().lower() == "text/event-stream"


@dataclass(frozen=True, slots=True)
class _SseEventTokenUpdate:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    response_id: str | None = None
    usage_seen: bool = False


def _build_usage_only_update(usage: dict[str, object]) -> _SseEventTokenUpdate:
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

    return _SseEventTokenUpdate(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        reasoning_tokens=reasoning_tokens,
        usage_seen=True,
    )


def _extract_usage_only_update_from_line(line: bytes) -> _SseEventTokenUpdate | None:
    stripped = line.strip()
    if not stripped.startswith(b"data: ") or stripped == b"data: [DONE]":
        return None

    usage = _extract_json_object_after_key(stripped, b'"usage"')
    if usage is None:
        return None

    return _build_usage_only_update(usage)


def _parse_truncated_sse_event_update(line: bytes) -> _SseEventTokenUpdate | None:
    if TRUNCATED_SSE_SENTINEL not in line:
        return None

    return _extract_usage_only_update_from_line(line)


def _parse_sse_event_update(line: bytes) -> _SseEventTokenUpdate | None:
    stripped = line.strip()
    if not stripped.startswith(b"data: ") or stripped == b"data: [DONE]":
        return None

    try:
        event_obj = cast(object, json.loads(stripped[6:]))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return _parse_truncated_sse_event_update(stripped)

    event = _as_object_dict(event_obj)
    if event is None:
        return None

    usage_seen = False
    input_tokens = None
    output_tokens = None
    total_tokens = None
    cache_read_input_tokens = None
    cache_creation_input_tokens = None
    reasoning_tokens = None
    response_id = None

    event_response_id = event.get("responseId")
    if isinstance(event_response_id, str) and event_response_id:
        response_id = event_response_id

    usage = _as_object_dict(event.get("usage"))
    if usage is None:
        response_payload = _as_object_dict(event.get("response"))
        if response_payload is not None:
            usage = _as_object_dict(response_payload.get("usage"))
            nested_response_id = response_payload.get("responseId")
            if response_id is None and isinstance(nested_response_id, str):
                response_id = nested_response_id

    if usage is not None:
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
        message = _as_object_dict(event.get("message"))
        msg_usage = (
            _as_object_dict(message.get("usage")) if message is not None else None
        )
        if msg_usage is not None:
            usage_seen = True
            input_tokens = _pick_int(msg_usage.get("input_tokens"), input_tokens)
            special_usage = _extract_special_usage(msg_usage)
            (
                cache_read_input_tokens,
                cache_creation_input_tokens,
                reasoning_tokens,
            ) = (
                _pick_int(special_usage[0], cache_read_input_tokens),
                _pick_int(special_usage[1], cache_creation_input_tokens),
                _pick_int(special_usage[2], reasoning_tokens),
            )

    if event.get("type") == "message_delta":
        delta_usage = _as_object_dict(event.get("usage"))
        if delta_usage is not None:
            usage_seen = True
            output_tokens = _pick_int(delta_usage.get("output_tokens"), output_tokens)
            special_usage = _extract_special_usage(delta_usage)
            (
                cache_read_input_tokens,
                cache_creation_input_tokens,
                reasoning_tokens,
            ) = (
                _pick_int(special_usage[0], cache_read_input_tokens),
                _pick_int(special_usage[1], cache_creation_input_tokens),
                _pick_int(special_usage[2], reasoning_tokens),
            )

    gemini_usage = _as_object_dict(event.get("usageMetadata"))
    if gemini_usage is not None:
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
        response_id=response_id,
        usage_seen=usage_seen,
    )


@dataclass(slots=True)
class _StreamingFinalizationBuffer:
    keep_payload: bool
    parse_sse_tokens: bool = True
    max_bytes: int = STREAMING_FINALIZATION_BUFFER_MAX_BYTES
    _payload: bytearray = field(default_factory=bytearray)
    _partial_line: bytearray = field(default_factory=bytearray)
    _payload_overflowed: bool = False
    _provider_correlation_id: str | None = None
    _tokens: TokenUsage | None = None
    _usage_seen: bool = False

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return

        if self.keep_payload:
            self._append_payload(chunk)

        if self.parse_sse_tokens:
            self._append_sse_chunk(chunk)

    def finalize(self) -> tuple[bytes | None, TokenUsage | None, str | None]:
        if self.parse_sse_tokens and self._partial_line:
            self._consume_line(bytes(self._partial_line))
            self._partial_line.clear()

        payload = None
        if self.keep_payload and not self._payload_overflowed:
            payload = bytes(self._payload)

        if self._tokens is None:
            return payload, None, self._provider_correlation_id

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

        return payload, finalized_tokens, self._provider_correlation_id

    def _append_payload(self, chunk: bytes) -> None:
        if self._payload_overflowed:
            return

        if len(self._payload) + len(chunk) > self.max_bytes:
            self._payload.clear()
            self._payload_overflowed = True
            return

        self._payload.extend(chunk)

    def _append_sse_chunk(self, chunk: bytes) -> None:
        if not chunk:
            return

        self._partial_line.extend(chunk)
        self._consume_complete_lines()
        self._truncate_partial_line_if_needed()

    def _truncate_partial_line_if_needed(self) -> None:
        if len(self._partial_line) <= self.max_bytes:
            return

        self._consume_partial_line_usage_hint()

        prefix_size = min(TRUNCATED_SSE_PREFIX_BYTES, self.max_bytes)
        tail_budget = self.max_bytes - prefix_size - len(TRUNCATED_SSE_SENTINEL)
        if tail_budget <= 0:
            self._partial_line = bytearray(self._partial_line[-self.max_bytes :])
            return

        prefix = bytes(self._partial_line[:prefix_size])
        tail = bytes(self._partial_line[-tail_budget:])
        self._partial_line = bytearray(prefix + TRUNCATED_SSE_SENTINEL + tail)

    def _consume_partial_line_usage_hint(self) -> None:
        update = _extract_usage_only_update_from_line(bytes(self._partial_line))
        if update is None:
            return
        self._apply_update(update)

    def _consume_complete_lines(self) -> None:
        lines = bytes(self._partial_line).split(b"\n")
        self._partial_line = bytearray(lines.pop())
        for line in lines:
            self._consume_line(line)

    def _consume_line(self, line: bytes) -> None:
        update = _parse_sse_event_update(line)
        if update is None:
            return
        self._apply_update(update)

    def _apply_update(self, update: _SseEventTokenUpdate) -> None:
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
        if self._provider_correlation_id is None and update.response_id is not None:
            self._provider_correlation_id = update.response_id
        self._usage_seen = self._usage_seen or update.usage_seen


def _extract_stream_tokens(payload: bytes) -> dict[str, int | None] | None:
    try:
        return extract_token_usage(payload)
    except Exception:
        logger.exception("Failed to extract streaming token usage")
        return None


def build_streaming_response(
    *,
    deps: ProxyRuntimeDependencies,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
    upstream_resp: httpx.Response,
    response_headers: dict[str, str],
    elapsed_ms: int,
) -> PreparedExecutionResponse:
    async def _release_stream_lease() -> None:
        if target.limiter_lease_token is None:
            return
        try:
            await deps.release_connection_lease_fn(
                profile_id=state.profile_id,
                lease_token=target.limiter_lease_token,
                now_at=None,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Failed to release streaming limiter lease: profile_id=%d connection_id=%d",
                state.profile_id,
                target.connection.id,
            )

    async def discard_response() -> None:
        try:
            await asyncio.shield(upstream_resp.aclose())
        except BaseException:
            pass
        await _release_stream_lease()

    async def commit_response(attempt_count: int) -> StreamingResponse:
        async def _iter_and_log(resp: httpx.Response):
            is_sse_stream = _is_sse_stream(
                resp.headers.get("content-type")
                if isinstance(resp.headers.get("content-type"), str)
                else None
            )
            finalization_buffer = _StreamingFinalizationBuffer(
                keep_payload=(
                    not is_sse_stream
                    or (state.setup.audit_enabled and state.setup.audit_capture_bodies)
                ),
                parse_sse_tokens=is_sse_stream,
            )
            heartbeat_stop = asyncio.Event()
            heartbeat_task: asyncio.Task[None] | None = None
            lease_ttl_seconds = target.limiter_lease_ttl_seconds
            if (
                target.limiter_lease_token is not None
                and lease_ttl_seconds is not None
                and lease_ttl_seconds > 0
            ):
                interval_seconds = max(float(lease_ttl_seconds) / 2.0, 0.1)

                async def _heartbeat_stream_lease() -> None:
                    while True:
                        try:
                            await asyncio.wait_for(
                                heartbeat_stop.wait(),
                                timeout=interval_seconds,
                            )
                            return
                        except asyncio.TimeoutError:
                            pass
                        await deps.heartbeat_connection_lease_fn(
                            profile_id=state.profile_id,
                            lease_token=target.limiter_lease_token,
                            lease_ttl_seconds=lease_ttl_seconds,
                            now_at=None,
                        )

                heartbeat_task = asyncio.create_task(_heartbeat_stream_lease())

            stream_cancelled = False
            stream_error: Exception | None = None
            stream_error_detail: str | None = None
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
                stream_error = exc
                stream_error_detail = str(exc)[:500]
                logger.warning("Stream iteration failed: %s", exc)
            finally:
                heartbeat_stop.set()
                if heartbeat_task is not None:
                    await asyncio.gather(heartbeat_task, return_exceptions=True)

                try:
                    await asyncio.shield(resp.aclose())
                except BaseException:
                    pass

                payload, token_usage, provider_correlation_id = (
                    finalization_buffer.finalize()
                )
                if token_usage is None and payload is not None:
                    token_usage = _extract_stream_tokens(payload)

                final_status_code = 0 if stream_error is not None else resp.status_code
                snapshot = build_stream_finalization_snapshot(
                    attempt_count=attempt_count,
                    deps=deps,
                    state=state,
                    target=target,
                    error_detail=stream_error_detail,
                    response_headers=response_headers,
                    status_code=final_status_code,
                    elapsed_ms=elapsed_ms,
                    payload=payload,
                    provider_correlation_id=provider_correlation_id,
                    token_usage=token_usage,
                )

                request_log_ready: asyncio.Future[int | None] | None = None
                try:
                    request_log_ready = enqueue_stream_finalize(snapshot)
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
                        _ = await persist_stream_request_log_inline_fallback(snapshot)
                    except asyncio.CancelledError:
                        logger.debug(
                            "Streaming request logging cancelled before completion"
                        )
                    except Exception:
                        logger.exception("Failed to log streaming request")

                if request_log_ready is not None and not stream_cancelled:
                    _ = await asyncio.shield(request_log_ready)

                try:
                    if stream_error is not None:
                        await deps.record_connection_failure_fn(
                            state.profile_id,
                            target.connection.id,
                            state.setup.failover_policy.failover_cooldown_seconds,
                            classify_failover_failure(exception=stream_error),
                            state.setup.failover_policy,
                            state.setup.model_id,
                            target.connection.endpoint_id,
                            state.setup.vendor_id,
                            now_at=None,
                        )
                    elif is_recovery_success_status(resp.status_code):
                        await deps.record_connection_recovery_fn(
                            state.profile_id,
                            target.connection.id,
                            state.setup.failover_policy,
                            state.setup.model_id,
                            target.connection.endpoint_id,
                            state.setup.vendor_id,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Failed to update streaming runtime state: profile_id=%d connection_id=%d",
                        state.profile_id,
                        target.connection.id,
                    )

                await _release_stream_lease()

        content_type_header = cast(
            str | None, upstream_resp.headers.get("content-type")
        )
        media_type = content_type_header or "text/event-stream"
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

    return PreparedExecutionResponse(
        commit_response_fn=commit_response,
        discard_response_fn=discard_response,
    )
