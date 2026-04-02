import logging
import time

import httpx
from fastapi.responses import Response, StreamingResponse

from app.services.stats_service import extract_token_usage

from .attempt_outcome_reporting import (
    log_and_audit_attempt,
    record_final_usage_event,
    response_error_detail,
)
from .attempt_streaming import build_streaming_response
from .attempt_types import (
    AttemptExecutionResult,
    PreparedExecutionResponse,
    ProxyAttemptTarget,
    ProxyRequestState,
    ProxyRuntimeDependencies,
)
from .proxy_request_helpers import classify_failover_failure, is_recovery_success_status

logger = logging.getLogger(__name__)


async def _release_limiter_lease_if_needed(
    *,
    deps: ProxyRuntimeDependencies,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
) -> None:
    if target.limiter_lease_token is None:
        return
    await deps.release_connection_lease_fn(
        profile_id=state.profile_id,
        lease_token=target.limiter_lease_token,
        now_at=None,
    )


async def _record_connection_recovery_if_needed(
    *,
    deps: ProxyRuntimeDependencies,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
    status_code: int,
) -> None:
    if (
        not state.setup.failover_policy.failover_recovery_enabled
        or not is_recovery_success_status(status_code)
    ):
        return
    await deps.record_connection_recovery_fn(
        state.profile_id,
        target.connection.id,
        state.setup.failover_policy,
        state.setup.model_id,
        target.connection.endpoint_id,
        state.setup.vendor_id,
    )


async def _record_connection_failure_if_needed(
    *,
    deps: ProxyRuntimeDependencies,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
    status_code: int | None = None,
    raw_body: bytes | None = None,
    exception: Exception | None = None,
) -> None:
    if not state.setup.failover_policy.failover_recovery_enabled:
        return
    failure_kind = classify_failover_failure(
        status_code=status_code,
        raw_body=raw_body,
        exception=exception,
    )
    await deps.record_connection_failure_fn(
        state.profile_id,
        target.connection.id,
        state.setup.failover_policy.failover_cooldown_seconds,
        failure_kind,
        state.setup.failover_policy,
        state.setup.model_id,
        target.connection.endpoint_id,
        state.setup.vendor_id,
        now_at=None,
    )


def _prepare_buffered_response(
    *,
    deps: ProxyRuntimeDependencies,
    elapsed_ms: int,
    error_detail: str | None,
    response_body: bytes,
    response_headers: dict[str, str],
    state: ProxyRequestState,
    status_code: int,
    target: ProxyAttemptTarget,
    tokens: dict[str, int | None] | None,
) -> PreparedExecutionResponse:
    async def commit_response(attempt_count: int) -> Response:
        _ = await log_and_audit_attempt(
            deps=deps,
            state=state,
            target=target,
            status_code=status_code,
            response_headers=response_headers,
            response_body=response_body,
            is_stream=False,
            elapsed_ms=elapsed_ms,
            error_detail=error_detail,
            tokens=tokens,
        )
        _ = await record_final_usage_event(
            deps=deps,
            state=state,
            target=target,
            status_code=status_code,
            attempt_count=attempt_count,
            tokens=tokens,
        )
        await _release_limiter_lease_if_needed(
            deps=deps,
            state=state,
            target=target,
        )
        await _record_connection_recovery_if_needed(
            deps=deps,
            state=state,
            target=target,
            status_code=status_code,
        )
        return Response(
            content=response_body,
            status_code=status_code,
            headers=response_headers,
        )

    async def discard_response() -> None:
        await _release_limiter_lease_if_needed(
            deps=deps,
            state=state,
            target=target,
        )

    return PreparedExecutionResponse(
        commit_response_fn=commit_response,
        discard_response_fn=discard_response,
    )


async def handle_streaming_attempt(
    *,
    deps: ProxyRuntimeDependencies,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
    client: httpx.AsyncClient,
    start_time: float,
) -> AttemptExecutionResult:
    if target.endpoint_body is None:
        send_request = client.build_request(
            state.setup.method,
            target.upstream_url,
            headers=target.headers,
        )
    else:
        send_request = client.build_request(
            state.setup.method,
            target.upstream_url,
            headers=target.headers,
            content=target.endpoint_body,
        )
    upstream_resp = await client.send(send_request, stream=True)

    response_headers = deps.filter_response_headers_fn(
        upstream_resp.headers,
        was_requested_compressed=state.setup.request_compressed,
    )
    elapsed_ms = int((time.monotonic() - start_time) * 1000)

    if upstream_resp.status_code >= 400:
        response_body = await upstream_resp.aread()
        await upstream_resp.aclose()
        error_detail = response_error_detail(response_body)

        if deps.should_failover_fn(
            upstream_resp.status_code,
            state.setup.failover_policy.failover_status_codes,
        ):
            logger.warning(
                "Endpoint %d failed with %d, trying next",
                target.connection.id,
                upstream_resp.status_code,
            )
            _ = await log_and_audit_attempt(
                deps=deps,
                state=state,
                target=target,
                status_code=upstream_resp.status_code,
                response_headers=response_headers,
                response_body=response_body,
                is_stream=True,
                elapsed_ms=elapsed_ms,
                error_detail=error_detail,
            )
            await _release_limiter_lease_if_needed(
                deps=deps,
                state=state,
                target=target,
            )
            await _record_connection_failure_if_needed(
                deps=deps,
                state=state,
                target=target,
                status_code=upstream_resp.status_code,
                raw_body=response_body,
            )
            return AttemptExecutionResult(
                attempted=True,
                accepted=False,
                error_detail=f"Upstream returned {upstream_resp.status_code}",
            )

        tokens = extract_token_usage(response_body)
        return AttemptExecutionResult(
            attempted=True,
            accepted=True,
            prepared_response=_prepare_buffered_response(
                deps=deps,
                elapsed_ms=elapsed_ms,
                error_detail=error_detail,
                response_body=response_body,
                response_headers=response_headers,
                state=state,
                status_code=upstream_resp.status_code,
                target=target,
                tokens=tokens,
            ),
        )

    return AttemptExecutionResult(
        attempted=True,
        accepted=True,
        prepared_response=build_streaming_response(
            deps=deps,
            state=state,
            target=target,
            upstream_resp=upstream_resp,
            response_headers=response_headers,
            elapsed_ms=elapsed_ms,
        ),
    )


async def handle_buffered_attempt(
    *,
    deps: ProxyRuntimeDependencies,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
    client: httpx.AsyncClient,
    start_time: float,
) -> AttemptExecutionResult:
    response = await deps.proxy_request_fn(
        client,
        state.setup.method,
        target.upstream_url,
        target.headers,
        target.endpoint_body,
    )
    elapsed_ms = int((time.monotonic() - start_time) * 1000)
    response_headers = deps.filter_response_headers_fn(
        response.headers,
        was_requested_compressed=state.setup.request_compressed,
    )

    if response.status_code >= 400 and deps.should_failover_fn(
        response.status_code,
        state.setup.failover_policy.failover_status_codes,
    ):
        logger.warning(
            "Endpoint %d failed with %d, trying next",
            target.connection.id,
            response.status_code,
        )
        _ = await log_and_audit_attempt(
            deps=deps,
            state=state,
            target=target,
            status_code=response.status_code,
            response_headers=response_headers,
            response_body=response.content,
            is_stream=False,
            elapsed_ms=elapsed_ms,
            error_detail=response_error_detail(response.content),
        )
        await _release_limiter_lease_if_needed(
            deps=deps,
            state=state,
            target=target,
        )
        await _record_connection_failure_if_needed(
            deps=deps,
            state=state,
            target=target,
            status_code=response.status_code,
            raw_body=response.content,
        )
        return AttemptExecutionResult(
            attempted=True,
            accepted=False,
            error_detail=f"Upstream returned {response.status_code}",
        )

    tokens = extract_token_usage(response.content)
    error_detail = None
    if response.status_code >= 400:
        error_detail = response_error_detail(response.content)

    return AttemptExecutionResult(
        attempted=True,
        accepted=True,
        prepared_response=_prepare_buffered_response(
            deps=deps,
            elapsed_ms=elapsed_ms,
            error_detail=error_detail,
            response_body=response.content,
            response_headers=response_headers,
            state=state,
            status_code=response.status_code,
            target=target,
            tokens=tokens,
        ),
    )


async def handle_transport_exception(
    *,
    deps: ProxyRuntimeDependencies,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
    start_time: float,
    exc: Exception,
) -> AttemptExecutionResult:
    elapsed_ms = int((time.monotonic() - start_time) * 1000)
    if isinstance(exc, httpx.ConnectError):
        last_error = f"Connection error: {exc}"
        logger.warning("Endpoint %d connection failed: %s", target.connection.id, exc)
    elif isinstance(exc, httpx.ReadError):
        last_error = f"Read error: {exc}"
        logger.warning("Endpoint %d read failed: %s", target.connection.id, exc)
    else:
        last_error = f"Timeout: {exc}"
        logger.warning("Endpoint %d timed out: %s", target.connection.id, exc)

    _ = await log_and_audit_attempt(
        deps=deps,
        state=state,
        target=target,
        status_code=0,
        response_headers=None,
        response_body=None,
        is_stream=state.setup.is_streaming,
        elapsed_ms=elapsed_ms,
        error_detail=last_error[:500],
    )
    await _release_limiter_lease_if_needed(
        deps=deps,
        state=state,
        target=target,
    )
    await _record_connection_failure_if_needed(
        deps=deps,
        state=state,
        target=target,
        exception=exc,
    )
    return AttemptExecutionResult(
        attempted=True,
        accepted=False,
        error_detail=last_error,
    )


__all__ = [
    "handle_buffered_attempt",
    "handle_streaming_attempt",
    "handle_transport_exception",
]
