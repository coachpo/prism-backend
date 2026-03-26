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
    ProxyAttemptTarget,
    ProxyRequestState,
    ProxyRuntimeDependencies,
)

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


def _parse_sse_event_update(line: bytes) -> _SseEventTokenUpdate | None:
    stripped = line.strip()
    if not stripped.startswith(b"data: ") or stripped == b"data: [DONE]":
        return None

    try:
        event_obj = cast(object, json.loads(stripped[6:]))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None

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
    _discard_until_newline: bool = False
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
        if (
            self.parse_sse_tokens
            and self._partial_line
            and not self._discard_until_newline
        ):
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
        remaining_chunk = chunk

        if self._discard_until_newline:
            newline_index = remaining_chunk.find(b"\n")
            if newline_index == -1:
                return
            remaining_chunk = remaining_chunk[newline_index + 1 :]
            self._discard_until_newline = False

        if not remaining_chunk:
            return

        self._partial_line.extend(remaining_chunk)
        self._consume_complete_lines()

        if len(self._partial_line) > self.max_bytes:
            self._partial_line.clear()
            self._discard_until_newline = True

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
        if self._provider_correlation_id is None and update.response_id is not None:
            self._provider_correlation_id = update.response_id
        self._usage_seen = self._usage_seen or update.usage_seen


def _extract_stream_tokens(payload: bytes) -> dict[str, int | None] | None:
    try:
        return extract_token_usage(payload)
    except Exception:
        logger.exception("Failed to extract streaming token usage")
        return None


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

            payload, token_usage, provider_correlation_id = (
                finalization_buffer.finalize()
            )
            if token_usage is None and payload is not None:
                token_usage = _extract_stream_tokens(payload)

            snapshot = build_stream_finalization_snapshot(
                deps=deps,
                state=state,
                target=target,
                response_headers=response_headers,
                status_code=resp.status_code,
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

            if target.limiter_lease_token is not None:
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

    content_type_header = cast(str | None, upstream_resp.headers.get("content-type"))
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
