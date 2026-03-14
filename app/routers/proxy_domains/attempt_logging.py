import time

from .attempt_types import (
    ProxyAttemptTarget,
    ProxyRequestState,
    ProxyRuntimeDependencies,
)
from .proxy_request_helpers import (
    _classify_failover_failure,
    _is_recovery_success_status,
)


def _response_error_detail(raw_body: bytes | None) -> str | None:
    if raw_body is None:
        return None
    return raw_body.decode("utf-8", errors="replace")[:500]


def _mark_connection_recovered_if_needed(
    *,
    deps: ProxyRuntimeDependencies,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
    status_code: int,
) -> None:
    if not state.setup.recovery_active or not _is_recovery_success_status(status_code):
        return
    deps.mark_connection_recovered_fn(
        state.profile_id,
        target.connection.id,
        state.setup.model_id,
        target.connection.endpoint_id,
        state.setup.provider_id,
    )


def _mark_connection_failed_if_needed(
    *,
    deps: ProxyRuntimeDependencies,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
    status_code: int | None = None,
    raw_body: bytes | None = None,
    exception: Exception | None = None,
) -> None:
    if not state.setup.recovery_active:
        return
    failure_kind = _classify_failover_failure(
        status_code=status_code,
        raw_body=raw_body,
        exception=exception,
    )
    deps.mark_connection_failed_fn(
        state.profile_id,
        target.connection.id,
        state.setup.model_config.failover_recovery_cooldown_seconds,
        time.monotonic(),
        failure_kind,
        state.setup.model_id,
        target.connection.endpoint_id,
        state.setup.provider_id,
    )


async def _record_request_log(
    *,
    deps: ProxyRuntimeDependencies,
    state: ProxyRequestState,
    target: ProxyAttemptTarget,
    status_code: int,
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


async def _record_attempt_audit(
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


async def _log_and_audit_attempt(
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
    request_log_id = await _record_request_log(
        deps=deps,
        state=state,
        target=target,
        status_code=status_code,
        elapsed_ms=elapsed_ms,
        is_stream=is_stream,
        error_detail=error_detail,
        tokens=tokens,
    )
    await _record_attempt_audit(
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
