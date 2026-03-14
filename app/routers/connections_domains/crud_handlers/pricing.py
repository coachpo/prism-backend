from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.models.models import Connection
from app.schemas.schemas import ConnectionPricingTemplateUpdate

from ..crud_dependencies import ConnectionCrudDependencies


async def set_connection_pricing_template_record(
    *,
    connection_id: int,
    body: ConnectionPricingTemplateUpdate,
    db: AsyncSession,
    profile_id: int,
    deps: ConnectionCrudDependencies,
) -> Connection:
    connection = await deps.load_connection_or_404_fn(
        db,
        profile_id=profile_id,
        connection_id=connection_id,
    )
    connection.pricing_template_id = await deps.validate_pricing_template_id_fn(
        db,
        profile_id=profile_id,
        pricing_template_id=body.pricing_template_id,
    )
    connection.updated_at = utc_now()
    await db.flush()

    return await deps.load_connection_or_404_fn(
        db,
        profile_id=profile_id,
        connection_id=connection.id,
    )


__all__ = ["set_connection_pricing_template_record"]
