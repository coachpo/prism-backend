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
    ProxyAttemptTarget,
    ProxyRequestState,
    ProxyRuntimeDependencies,
)

logger = logging.getLogger(__name__)


def _classify_failover_failure(
    *,
    status_code: int | None = None,
    raw_body: bytes | None = None,
    exception: Exception | None = None,
) -> str:
    if exception is not None:
        return (
            "timeout"
            if isinstance(exception, httpx.TimeoutException)
            else "connect_error"
        )
    if status_code is None:
        return "transient_http"
    return "transient_http"


def _is_recovery_success_status(status_code: int) -> bool:
    return 200 <= status_code < 400


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
        or not _is_recovery_success_status(status_code)
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
    failure_kind = _classify_failover_failure(
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


async def handle_streaming_attempt(
    *,
    deps: ProxyRuntimeDependencies,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
    client: httpx.AsyncClient,
    start_time: float,
) -> tuple[Response | StreamingResponse | None, str | None]:
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
            last_error = f"Upstream returned {upstream_resp.status_code}"
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
            return None, last_error

        tokens = extract_token_usage(response_body)
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
            tokens=tokens,
        )
        _ = await record_final_usage_event(
            deps=deps,
            state=state,
            target=target,
            status_code=upstream_resp.status_code,
            attempt_count=target.attempt_number,
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
            status_code=upstream_resp.status_code,
        )
        return (
            Response(
                content=response_body,
                status_code=upstream_resp.status_code,
                headers=response_headers,
            ),
            None,
        )

    await _record_connection_recovery_if_needed(
        deps=deps,
        state=state,
        target=target,
        status_code=upstream_resp.status_code,
    )
    return (
        await build_streaming_response(
            deps=deps,
            state=state,
            target=target,
            upstream_resp=upstream_resp,
            response_headers=response_headers,
            elapsed_ms=elapsed_ms,
        ),
        None,
    )


async def handle_buffered_attempt(
    *,
    deps: ProxyRuntimeDependencies,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
    client: httpx.AsyncClient,
    start_time: float,
) -> tuple[Response | None, str | None]:
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
        last_error = f"Upstream returned {response.status_code}"
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
        return None, last_error

    tokens = extract_token_usage(response.content)
    error_detail = None
    if response.status_code >= 400:
        error_detail = response_error_detail(response.content)

    _ = await log_and_audit_attempt(
        deps=deps,
        state=state,
        target=target,
        status_code=response.status_code,
        response_headers=response_headers,
        response_body=response.content,
        is_stream=False,
        elapsed_ms=elapsed_ms,
        error_detail=error_detail,
        tokens=tokens,
    )
    _ = await record_final_usage_event(
        deps=deps,
        state=state,
        target=target,
        status_code=response.status_code,
        attempt_count=target.attempt_number,
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
        status_code=response.status_code,
    )
    return (
        Response(
            content=response.content,
            status_code=response.status_code,
            headers=response_headers,
        ),
        None,
    )


async def handle_transport_exception(
    *,
    deps: ProxyRuntimeDependencies,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
    start_time: float,
    exc: Exception,
) -> str:
    elapsed_ms = int((time.monotonic() - start_time) * 1000)
    if isinstance(exc, httpx.ConnectError):
        last_error = f"Connection error: {exc}"
        logger.warning("Endpoint %d connection failed: %s", target.connection.id, exc)
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
        error_detail=str(exc)[:500],
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
    return last_error
