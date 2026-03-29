from collections.abc import Awaitable, Callable

import httpx
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.schemas import HealthCheckResponse
from app.services.monitoring_service import ProbeExecutionResult


async def perform_connection_health_check(
    *,
    connection_id: int,
    request: Request,
    db: AsyncSession,
    profile_id: int,
    run_connection_probe_fn: Callable[..., Awaitable[ProbeExecutionResult]],
) -> HealthCheckResponse:
    client: httpx.AsyncClient = request.app.state.http_client
    result = await run_connection_probe_fn(
        db=db,
        client=client,
        profile_id=profile_id,
        connection_id=connection_id,
    )
    return HealthCheckResponse(
        connection_id=result.connection_id,
        health_status=result.health_status,
        checked_at=result.checked_at,
        detail=result.detail,
        response_time_ms=result.conversation_delay_ms or result.endpoint_ping_ms or 0,
    )


__all__ = ["perform_connection_health_check"]
