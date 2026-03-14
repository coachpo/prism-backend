import logging
from typing import Awaitable, Callable

import httpx
from fastapi import HTTPException, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.time import utc_now
from app.models.models import Connection, HeaderBlocklistRule, ModelConfig
from app.schemas.schemas import HealthCheckResponse

logger = logging.getLogger(__name__)


async def _load_health_check_connection_or_404(
    db: AsyncSession,
    *,
    profile_id: int,
    connection_id: int,
) -> Connection:
    result = await db.execute(
        select(Connection)
        .options(
            selectinload(Connection.model_config_rel).selectinload(
                ModelConfig.provider
            ),
            selectinload(Connection.endpoint_rel),
        )
        .where(
            Connection.id == connection_id,
            Connection.profile_id == profile_id,
        )
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    return connection


async def _load_enabled_blocklist_rules(
    db: AsyncSession,
    *,
    profile_id: int,
) -> list[HeaderBlocklistRule]:
    result = await db.execute(
        select(HeaderBlocklistRule).where(
            HeaderBlocklistRule.enabled == True,  # noqa: E712
            or_(
                HeaderBlocklistRule.is_system == True,  # noqa: E712
                HeaderBlocklistRule.profile_id == profile_id,
            ),
        )
    )
    return list(result.scalars().all())


async def perform_connection_health_check(
    *,
    connection_id: int,
    request: Request,
    db: AsyncSession,
    profile_id: int,
    build_upstream_headers_fn: Callable[..., dict[str, str]],
    probe_connection_health_fn: Callable[..., Awaitable[tuple[str, str, int, str]]],
    mark_connection_recovered_fn: Callable[..., None],
) -> HealthCheckResponse:
    connection = await _load_health_check_connection_or_404(
        db,
        profile_id=profile_id,
        connection_id=connection_id,
    )
    endpoint = connection.endpoint_rel
    if endpoint is None:
        raise HTTPException(status_code=400, detail="Connection endpoint is missing")

    provider = connection.model_config_rel.provider
    provider_type = provider.provider_type
    model_id = connection.model_config_rel.model_id
    blocklist_rules = await _load_enabled_blocklist_rules(db, profile_id=profile_id)
    headers = build_upstream_headers_fn(
        connection,
        provider_type,
        blocklist_rules=blocklist_rules,
        endpoint=endpoint,
    )

    checked_at = utc_now()
    client: httpx.AsyncClient = request.app.state.http_client
    health_status, detail, response_time_ms, log_url = await probe_connection_health_fn(
        client=client,
        connection=connection,
        endpoint=endpoint,
        provider_type=provider_type,
        model_id=model_id,
        headers=headers,
    )

    logger.info(
        "Health check connection_id=%d endpoint_id=%d url=%s status=%s detail=%s",
        connection.id,
        endpoint.id,
        log_url,
        health_status,
        detail,
    )

    connection.health_status = health_status
    connection.health_detail = detail
    connection.last_health_check = checked_at
    if health_status == "healthy":
        mark_connection_recovered_fn(profile_id, connection.id)

    await db.flush()

    return HealthCheckResponse(
        connection_id=connection.id,
        health_status=health_status,
        checked_at=checked_at,
        detail=detail,
        response_time_ms=response_time_ms,
    )


__all__ = ["perform_connection_health_check"]
