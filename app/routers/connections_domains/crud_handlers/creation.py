from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Connection
from app.schemas.schemas import ConnectionCreate

from ..crud_dependencies import ConnectionCrudDependencies
from .shared import build_connection_limiter_data, resolve_create_endpoint


async def create_connection_record(
    *,
    model_config_id: int,
    body: ConnectionCreate,
    db: AsyncSession,
    profile_id: int,
    deps: ConnectionCrudDependencies,
) -> Connection:
    model_config = await deps.load_model_or_404_fn(
        db,
        profile_id=profile_id,
        model_config_id=model_config_id,
    )
    endpoint = await resolve_create_endpoint(
        body=body,
        db=db,
        profile_id=profile_id,
        deps=deps,
    )
    pricing_template_id = await deps.validate_pricing_template_id_fn(
        db,
        profile_id=profile_id,
        pricing_template_id=body.pricing_template_id,
    )

    await deps.lock_profile_row_fn(db, profile_id=profile_id)
    ordered_connections = await deps.list_ordered_connections_fn(
        db,
        profile_id=profile_id,
        model_config_id=model_config_id,
    )
    deps.normalize_connection_priorities_fn(ordered_connections)
    await db.flush()

    connection = Connection(
        profile_id=profile_id,
        model_config_id=model_config_id,
        endpoint_id=endpoint.id,
        is_active=body.is_active,
        priority=len(ordered_connections),
        name=body.name,
        auth_type=body.auth_type,
        custom_headers=deps.serialize_custom_headers_fn(body.custom_headers),
        monitoring_probe_interval_seconds=body.monitoring_probe_interval_seconds,
        openai_probe_endpoint_variant=(
            body.openai_probe_endpoint_variant
            if getattr(model_config, "api_family", None) == "openai"
            else "responses"
        ),
        pricing_template_id=pricing_template_id,
        **build_connection_limiter_data(body=body, exclude_unset=False),
    )
    db.add(connection)
    await db.flush()
    await deps.clear_round_robin_state_for_model_fn(
        db,
        profile_id=profile_id,
        model_config_id=model_config_id,
    )

    return await deps.load_connection_or_404_fn(
        db,
        profile_id=profile_id,
        connection_id=connection.id,
    )


__all__ = ["create_connection_record"]
