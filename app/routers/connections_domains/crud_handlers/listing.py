from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Connection

from ..crud_dependencies import ConnectionCrudDependencies


async def list_connections_for_model(
    *,
    model_config_id: int,
    db: AsyncSession,
    profile_id: int,
    deps: ConnectionCrudDependencies,
) -> list[Connection]:
    await deps.load_model_or_404_fn(
        db,
        profile_id=profile_id,
        model_config_id=model_config_id,
    )
    return await deps.list_ordered_connections_fn(
        db,
        profile_id=profile_id,
        model_config_id=model_config_id,
    )


__all__ = ["list_connections_for_model"]
