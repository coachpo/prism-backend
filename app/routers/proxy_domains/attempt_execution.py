import asyncio
import logging
import time
from typing import Awaitable, Callable, cast

import httpx
from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Connection
from app.services.loadbalancer.executor import execute_deadline_aware_attempts

from .attempt_handlers import (
    handle_buffered_attempt,
    handle_streaming_attempt,
    handle_transport_exception,
)
from .attempt_outcome_reporting import record_final_usage_event
from .attempt_types import (
    AttemptExecutionResult,
    ExecutionCandidate,
    ProxyAttemptTarget,
    ProxyRequestState,
    ProxyRuntimeDependencies,
)
from .request_setup import ProxyRequestSetup

logger = logging.getLogger(__name__)

DEFAULT_LIMITER_LEASE_TTL_SECONDS = 30


def _connection_has_limiter_config(connection: Connection) -> bool:
    for field_name in (
        "qps_limit",
        "max_in_flight_non_stream",
        "max_in_flight_stream",
    ):
        value = getattr(connection, field_name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return True
    return False


def _build_attempt_target(
    *,
    attempt_number: int,
    connection: Connection,
    limiter_lease_token: str | None,
    limiter_lease_ttl_seconds: int | None,
    request_query: str | None,
    setup: ProxyRequestSetup,
    deps: ProxyRuntimeDependencies,
) -> ProxyAttemptTarget:
    upstream_url = deps.build_upstream_url_fn(
        connection,
        setup.effective_request_path,
        endpoint=connection.endpoint_rel,
    )
    if request_query:
        upstream_url = f"{upstream_url}?{request_query}"

    return ProxyAttemptTarget(
        attempt_number=attempt_number,
        connection=connection,
        description=connection.name or f"Connection {connection.id}",
        endpoint_body=setup.rewritten_body,
        headers=deps.build_upstream_headers_fn(
            connection,
            setup.api_family,
            setup.client_headers,
            setup.blocklist_rules,
            endpoint=connection.endpoint_rel,
            request_compressed=setup.request_compressed,
        ),
        limiter_lease_token=limiter_lease_token,
        limiter_lease_ttl_seconds=limiter_lease_ttl_seconds,
        upstream_url=upstream_url,
    )


async def execute_proxy_attempts(
    *,
    db: AsyncSession,
    endpoint_is_active_now_fn: Callable[..., Awaitable[bool]],
    request_path: str,
    request_query: str | None,
    profile_id: int,
    setup: ProxyRequestSetup,
    deps: ProxyRuntimeDependencies,
) -> Response | StreamingResponse:
    last_attempt_target: ProxyAttemptTarget | None = None
    state = ProxyRequestState(
        profile_id=profile_id,
        request_path=request_path,
        setup=setup,
    )

    async def run_attempt(
        candidate: ExecutionCandidate,
        attempt_number: int,
    ) -> AttemptExecutionResult:
        nonlocal last_attempt_target
        connection = candidate.connection
        if connection.endpoint_rel is None:
            logger.warning(
                "Skipping connection %d because endpoint is missing",
                connection.id,
            )
            return AttemptExecutionResult(
                attempted=False,
                accepted=False,
                error_detail=f"Connection {connection.id} is missing an endpoint",
            )

        if not await endpoint_is_active_now_fn(db, connection.id):
            await deps.clear_connection_state_fn(
                profile_id,
                connection.id,
            )
            logger.info(
                "Skipping endpoint %d because it is currently disabled",
                connection.id,
            )
            return AttemptExecutionResult(
                attempted=False,
                accepted=False,
                error_detail=f"Connection {connection.id} is disabled",
            )

        if candidate.probe_eligible:
            await deps.claim_probe_eligible_fn(
                profile_id=profile_id,
                connection_id=connection.id,
                model_id=setup.model_id,
                endpoint_id=connection.endpoint_id,
                policy=setup.failover_policy,
                vendor_id=setup.vendor_id,
                now_at=None,
            )

        limiter_lease_token = None
        limiter_lease_ttl_seconds: int | None = None
        if _connection_has_limiter_config(connection):
            limiter_lease_ttl_seconds = DEFAULT_LIMITER_LEASE_TTL_SECONDS
            limiter_result = await deps.acquire_connection_limit_fn(
                profile_id=profile_id,
                connection=connection,
                lease_kind="stream" if setup.is_streaming else "non_stream",
                lease_ttl_seconds=limiter_lease_ttl_seconds,
                now_at=None,
            )
            if not limiter_result.admitted:
                logger.info(
                    "Connection %d rejected by limiter: %s",
                    connection.id,
                    limiter_result.deny_reason,
                )
                return AttemptExecutionResult(
                    attempted=False,
                    accepted=False,
                    limiter_denied=True,
                    error_detail=(
                        f"Connection {connection.id} rejected by limiter: "
                        f"{limiter_result.deny_reason}"
                    ),
                )
            limiter_lease_token = limiter_result.lease_token

        target = _build_attempt_target(
            attempt_number=attempt_number,
            connection=connection,
            limiter_lease_token=limiter_lease_token,
            limiter_lease_ttl_seconds=limiter_lease_ttl_seconds,
            request_query=request_query,
            setup=setup,
            deps=deps,
        )
        last_attempt_target = target
        start_time = time.monotonic()

        try:
            if setup.is_streaming:
                return await handle_streaming_attempt(
                    deps=deps,
                    state=state,
                    target=target,
                    client=setup.client,
                    start_time=start_time,
                )
            return await handle_buffered_attempt(
                deps=deps,
                state=state,
                target=target,
                client=setup.client,
                start_time=start_time,
            )
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
            return await handle_transport_exception(
                deps=deps,
                state=state,
                target=target,
                start_time=start_time,
                exc=exc,
            )
        except asyncio.CancelledError:
            if limiter_lease_token is not None:
                await deps.release_connection_lease_fn(
                    profile_id=profile_id,
                    lease_token=limiter_lease_token,
                    now_at=None,
                )
            raise

    execution_result = await execute_deadline_aware_attempts(
        db=db,
        profile_id=profile_id,
        model_config=setup.model_config,
        policy=setup.failover_policy,
        initial_candidates=setup.initial_candidates,
        is_streaming=setup.is_streaming,
        request_deadline_at_monotonic=setup.request_deadline_at_monotonic,
        run_attempt_fn=run_attempt,
    )

    if execution_result.response is not None:
        return cast(Response | StreamingResponse, execution_result.response)

    final_status_code = 504 if execution_result.deadline_exhausted else 502
    if last_attempt_target is not None:
        _ = await record_final_usage_event(
            deps=deps,
            state=state,
            target=last_attempt_target,
            status_code=final_status_code,
            attempt_count=max(execution_result.attempt_count, 1),
        )

    if execution_result.deadline_exhausted:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Request deadline exhausted for model '{setup.model_id}'. "
                f"Last error: {execution_result.last_error}"
            ),
        )

    if not execution_result.attempted_any_endpoint:
        if execution_result.limiter_denied_any_endpoint:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"All connections for model '{setup.model_id}' are currently "
                    "rate-limited or saturated."
                ),
            )
        raise HTTPException(
            status_code=503,
            detail=f"No active connections available for model '{setup.model_id}'.",
        )

    raise HTTPException(
        status_code=502,
        detail=(
            f"All connections failed for model '{setup.model_id}'. "
            f"Last error: {execution_result.last_error}"
        ),
    )


__all__ = ["ProxyRuntimeDependencies", "execute_proxy_attempts"]
