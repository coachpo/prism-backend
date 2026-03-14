from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Connection
from app.schemas.schemas import ConnectionPriorityMoveRequest

from ..crud_dependencies import ConnectionCrudDependencies


async def move_connection_priority_for_model(
    *,
    model_config_id: int,
    connection_id: int,
    body: ConnectionPriorityMoveRequest,
    db: AsyncSession,
    profile_id: int,
    deps: ConnectionCrudDependencies,
) -> list[Connection]:
    await deps.load_model_or_404_fn(
        db,
        profile_id=profile_id,
        model_config_id=model_config_id,
    )
    await deps.lock_profile_row_fn(db, profile_id=profile_id)
    connections = await deps.list_ordered_connections_fn(
        db,
        profile_id=profile_id,
        model_config_id=model_config_id,
    )
    current_index = next(
        (
            index
            for index, connection in enumerate(connections)
            if connection.id == connection_id
        ),
        None,
    )
    if current_index is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    if body.to_index >= len(connections):
        raise HTTPException(
            status_code=422,
            detail=f"to_index must be between 0 and {len(connections) - 1}",
        )

    if body.to_index == current_index:
        deps.normalize_connection_priorities_fn(connections)
        await db.flush()
        return connections

    connection = connections.pop(current_index)
    connections.insert(body.to_index, connection)
    deps.normalize_connection_priorities_fn(connections)
    await db.flush()
    return connections


__all__ = ["move_connection_priority_for_model"]
