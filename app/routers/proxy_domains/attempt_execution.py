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


def _build_attempt_target(
    *,
    connection: Connection,
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
        connection=connection,
        description=connection.name,
        endpoint_body=setup.rewritten_body,
        headers=deps.build_upstream_headers_fn(
            connection,
            setup.provider_type,
            setup.client_headers,
            setup.blocklist_rules,
            endpoint=connection.endpoint_rel,
            request_compressed=setup.request_compressed,
        ),
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
    state = ProxyRequestState(
        profile_id=profile_id,
        request_path=request_path,
        setup=setup,
    )

    for connection in setup.endpoints_to_try:
        if connection.endpoint_rel is None:
            logger.warning(
                "Skipping connection %d because endpoint is missing",
                connection.id,
            )
            continue

        if not await endpoint_is_active_now_fn(db, connection.id):
            deps.mark_connection_recovered_fn(
                profile_id,
                connection.id,
                setup.model_id,
                connection.endpoint_id,
                setup.provider_id,
            )
            logger.info(
                "Skipping endpoint %d because it is currently disabled",
                connection.id,
            )
            continue

        attempted_any_endpoint = True
        target = _build_attempt_target(
            connection=connection,
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
