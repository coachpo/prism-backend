from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.models.models import Connection
from app.schemas.schemas import ConnectionUpdate

from ..crud_dependencies import ConnectionCrudDependencies
from .shared import build_connection_update_data, should_clear_recovery_state


async def update_connection_record(
    *,
    connection_id: int,
    body: ConnectionUpdate,
    db: AsyncSession,
    profile_id: int,
    deps: ConnectionCrudDependencies,
) -> Connection:
    connection = await deps.load_connection_or_404_fn(
        db,
        profile_id=profile_id,
        connection_id=connection_id,
    )
    update_data = await build_connection_update_data(
        body=body,
        db=db,
        profile_id=profile_id,
        deps=deps,
    )
    clear_recovery_state = should_clear_recovery_state(connection, update_data)

    for key, value in update_data.items():
        setattr(connection, key, value)

    if clear_recovery_state:
        deps.mark_connection_recovered_fn(profile_id, connection.id)
    connection.updated_at = utc_now()
    await db.flush()

    return await deps.load_connection_or_404_fn(
        db,
        profile_id=profile_id,
        connection_id=connection.id,
    )


__all__ = ["update_connection_record"]
