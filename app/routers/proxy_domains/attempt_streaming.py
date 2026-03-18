import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

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
    payload: bytes
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
    upstream_url: str


def _build_stream_finalization_snapshot(
    *,
    deps: ProxyRuntimeDependencies,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
    response_headers: dict[str, str],
    status_code: int,
    elapsed_ms: int,
    payload: bytes,
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
        response_body=snapshot.payload,
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
        accumulated = bytearray()
        stream_cancelled = False
        try:
            async for chunk in resp.aiter_bytes():
                if chunk:
                    accumulated.extend(chunk)
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

            snapshot = _build_stream_finalization_snapshot(
                deps=deps,
                state=state,
                target=target,
                response_headers=response_headers,
                status_code=resp.status_code,
                elapsed_ms=elapsed_ms,
                payload=bytes(accumulated),
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
