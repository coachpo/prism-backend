import logging
import time
from typing import Awaitable, Callable

import httpx
from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Connection

from .attempt_handlers import (
    handle_buffered_attempt,
    handle_streaming_attempt,
    handle_transport_exception,
)
from .attempt_types import (
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
    last_error = None
    attempted_any_endpoint = False
    limiter_denied_any_endpoint = False
    state = ProxyRequestState(
        profile_id=profile_id,
        request_path=request_path,
        setup=setup,
    )

    for attempt_number, connection in enumerate(setup.endpoints_to_try, start=1):
        if connection.endpoint_rel is None:
            logger.warning(
                "Skipping connection %d because endpoint is missing",
                connection.id,
            )
            continue

        if not await endpoint_is_active_now_fn(db, connection.id):
            await deps.clear_connection_state_fn(
                profile_id,
                connection.id,
            )
            logger.info(
                "Skipping endpoint %d because it is currently disabled",
                connection.id,
            )
            continue

        if connection.id in setup.probe_eligible_connection_ids:
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
        if _connection_has_limiter_config(connection):
            limiter_result = await deps.acquire_connection_limit_fn(
                profile_id=profile_id,
                connection=connection,
                lease_kind="stream" if setup.is_streaming else "non_stream",
                lease_ttl_seconds=DEFAULT_LIMITER_LEASE_TTL_SECONDS,
                now_at=None,
            )
            if not limiter_result.admitted:
                limiter_denied_any_endpoint = True
                last_error = (
                    f"Connection {connection.id} rejected by limiter: "
                    f"{limiter_result.deny_reason}"
                )
                logger.info(last_error)
                continue
            limiter_lease_token = limiter_result.lease_token

        attempted_any_endpoint = True
        target = _build_attempt_target(
            attempt_number=attempt_number,
            connection=connection,
            limiter_lease_token=limiter_lease_token,
            request_query=request_query,
            setup=setup,
            deps=deps,
        )
        start_time = time.monotonic()

        try:
            if setup.is_streaming:
                response, last_error = await handle_streaming_attempt(
                    deps=deps,
                    state=state,
                    target=target,
                    client=setup.client,
                    start_time=start_time,
                )
            else:
                response, last_error = await handle_buffered_attempt(
                    deps=deps,
                    state=state,
                    target=target,
                    client=setup.client,
                    start_time=start_time,
                )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_error = await handle_transport_exception(
                deps=deps,
                state=state,
                target=target,
                start_time=start_time,
                exc=exc,
            )
            continue

        if response is not None:
            return response

    if not attempted_any_endpoint:
        if limiter_denied_any_endpoint:
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
            f"Last error: {last_error}"
        ),
    )


__all__ = ["ProxyRuntimeDependencies", "execute_proxy_attempts"]
