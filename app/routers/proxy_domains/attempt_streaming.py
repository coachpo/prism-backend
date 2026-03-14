import asyncio
import logging

import httpx
from fastapi.responses import StreamingResponse

from app.services.stats_service import extract_token_usage

from .attempt_logging import _record_attempt_audit, _record_request_log
from .attempt_types import (
    ProxyAttemptTarget,
    ProxyRequestState,
    ProxyRuntimeDependencies,
)
from .proxy_request_helpers import _track_detached_task

logger = logging.getLogger(__name__)


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

            payload = bytes(accumulated)

            async def _finalize_stream() -> None:
                request_log_id = None
                try:
                    tokens = extract_token_usage(payload)
                    request_log_id = await _record_request_log(
                        deps=deps,
                        state=state,
                        target=target,
                        status_code=resp.status_code,
                        elapsed_ms=elapsed_ms,
                        is_stream=True,
                        tokens=tokens,
                    )
                except asyncio.CancelledError:
                    logger.debug(
                        "Streaming request logging cancelled before completion"
                    )
                    return
                except Exception:
                    logger.exception("Failed to log streaming request")

                try:
                    await _record_attempt_audit(
                        deps=deps,
                        request_log_id=request_log_id,
                        state=state,
                        target=target,
                        status_code=resp.status_code,
                        response_headers=response_headers,
                        response_body=payload,
                        is_stream=True,
                        elapsed_ms=elapsed_ms,
                    )
                except asyncio.CancelledError:
                    logger.debug("Streaming audit logging cancelled before completion")
                except Exception:
                    logger.exception("Failed to record streaming audit log")

            try:
                finalize_task = asyncio.create_task(
                    _finalize_stream(),
                    name="proxy-stream-finalize",
                )
            except RuntimeError:
                logger.debug(
                    "Event loop closed before stream finalization could be scheduled"
                )
            else:
                if stream_cancelled:
                    _track_detached_task(
                        finalize_task,
                        name="proxy stream finalization",
                    )
                else:
                    try:
                        await asyncio.shield(finalize_task)
                    except asyncio.CancelledError:
                        _track_detached_task(
                            finalize_task,
                            name="proxy stream finalization",
                        )
                        raise

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
