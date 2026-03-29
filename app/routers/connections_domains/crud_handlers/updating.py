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
    model_config = await deps.load_model_or_404_fn(
        db,
        profile_id=profile_id,
        model_config_id=connection.model_config_id,
    )
    update_data = await build_connection_update_data(
        body=body,
        db=db,
        profile_id=profile_id,
        deps=deps,
    )
    if (
        "openai_probe_endpoint_variant" in update_data
        and getattr(model_config, "api_family", None) != "openai"
    ):
        update_data["openai_probe_endpoint_variant"] = "responses"
    clear_recovery_state = should_clear_recovery_state(connection, update_data)
    clear_round_robin_state = (
        "is_active" in update_data and update_data["is_active"] != connection.is_active
    )

    for key, value in update_data.items():
        setattr(connection, key, value)

    if clear_recovery_state:
        await deps.clear_connection_state_fn(profile_id, connection.id)
    if clear_round_robin_state:
        await deps.clear_round_robin_state_for_model_fn(
            db,
            profile_id=profile_id,
            model_config_id=connection.model_config_id,
        )
    connection.updated_at = utc_now()
    await db.flush()

    return await deps.load_connection_or_404_fn(
        db,
        profile_id=profile_id,
        connection_id=connection.id,
    )


__all__ = ["update_connection_record"]
