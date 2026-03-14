from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.models import Connection
from app.schemas.schemas import ConnectionOwnerResponse


async def _load_connection_owner_or_404(
    db: AsyncSession,
    *,
    profile_id: int,
    connection_id: int,
) -> Connection:
    result = await db.execute(
        select(Connection)
        .options(
            selectinload(Connection.model_config_rel),
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


async def get_connection_owner_details(
    *,
    connection_id: int,
    db: AsyncSession,
    profile_id: int,
) -> ConnectionOwnerResponse:
    connection = await _load_connection_owner_or_404(
        db,
        profile_id=profile_id,
        connection_id=connection_id,
    )
    if connection.endpoint_rel is None:
        raise HTTPException(status_code=400, detail="Connection endpoint is missing")

    return ConnectionOwnerResponse(
        connection_id=connection.id,
        model_config_id=connection.model_config_id,
        model_id=connection.model_config_rel.model_id,
        connection_name=connection.name,
        endpoint_id=connection.endpoint_rel.id,
        endpoint_name=connection.endpoint_rel.name,
        endpoint_base_url=connection.endpoint_rel.base_url,
    )


__all__ = ["get_connection_owner_details"]
