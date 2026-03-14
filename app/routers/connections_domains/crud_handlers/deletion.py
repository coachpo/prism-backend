from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Connection

from ..crud_dependencies import ConnectionCrudDependencies


async def delete_connection_record(
    *,
    connection_id: int,
    db: AsyncSession,
    profile_id: int,
    deps: ConnectionCrudDependencies,
) -> dict[str, bool]:
    await deps.lock_profile_row_fn(db, profile_id=profile_id)
    connection_result = await db.execute(
        select(Connection).where(
            Connection.id == connection_id,
            Connection.profile_id == profile_id,
        )
    )
    connection = connection_result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    model_config_id = connection.model_config_id
    deps.mark_connection_recovered_fn(profile_id, connection.id)
    await db.delete(connection)
    await db.flush()

    remaining_connections = await deps.list_ordered_connections_fn(
        db,
        profile_id=profile_id,
        model_config_id=model_config_id,
    )
    deps.normalize_connection_priorities_fn(remaining_connections)
    await db.flush()
    return {"deleted": True}


__all__ = ["delete_connection_record"]
