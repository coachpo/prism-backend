import json

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.crypto import encrypt_secret
from app.models.models import (
    Connection,
    Endpoint,
    ModelConfig,
    PricingTemplate,
)
from app.routers.shared import (
    ensure_unique_endpoint_name as _ensure_unique_endpoint_name,
    get_next_endpoint_position as _get_next_endpoint_position,
    lock_profile_row as _lock_profile_row,
    normalize_ordered_field,
)
from app.services.proxy_service import normalize_base_url, validate_base_url


async def _create_endpoint_from_inline(
    db: AsyncSession,
    *,
    profile_id: int,
    endpoint_name: str,
    base_url: str,
    api_key: str,
) -> Endpoint:
    clean_name = endpoint_name.strip()
    if not clean_name:
        raise HTTPException(
            status_code=422, detail="endpoint_create.name must not be empty"
        )

    normalized_url = normalize_base_url(base_url)
    url_warnings = validate_base_url(normalized_url)
    if url_warnings:
        raise HTTPException(status_code=422, detail="; ".join(url_warnings))

    await _lock_profile_row(db, profile_id=profile_id)
    await _ensure_unique_endpoint_name(
        db,
        profile_id=profile_id,
        endpoint_name=clean_name,
    )

    endpoint = Endpoint(
        profile_id=profile_id,
        name=clean_name,
        base_url=normalized_url,
        api_key=encrypt_secret(api_key),
        position=await _get_next_endpoint_position(db, profile_id=profile_id),
    )
    db.add(endpoint)
    await db.flush()
    return endpoint


async def _validate_pricing_template_id(
    db: AsyncSession,
    *,
    profile_id: int,
    pricing_template_id: int | None,
    allow_null: bool = True,
) -> int | None:
    if pricing_template_id is None:
        if allow_null:
            return None
        raise HTTPException(status_code=422, detail="pricing_template_id is required")

    template_result = await db.execute(
        select(PricingTemplate).where(
            PricingTemplate.id == pricing_template_id,
            PricingTemplate.profile_id == profile_id,
        )
    )
    template = template_result.scalar_one_or_none()
    if template is None:
        raise HTTPException(status_code=404, detail="Pricing template not found")
    return template.id


async def _load_connection_or_404(
    db: AsyncSession,
    *,
    profile_id: int,
    connection_id: int,
) -> Connection:
    result = await db.execute(
        select(Connection)
        .options(
            selectinload(Connection.endpoint_rel),
            selectinload(Connection.pricing_template_rel),
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


async def _load_model_or_404(
    db: AsyncSession,
    *,
    profile_id: int,
    model_config_id: int,
) -> ModelConfig:
    result = await db.execute(
        select(ModelConfig).where(
            ModelConfig.id == model_config_id,
            ModelConfig.profile_id == profile_id,
        )
    )
    model = result.scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=404, detail="Model configuration not found")
    return model


async def _ensure_model_config_ids_exist(
    db: AsyncSession,
    *,
    profile_id: int,
    model_config_ids: list[int],
) -> None:
    normalized_model_config_ids = list(dict.fromkeys(model_config_ids))
    if not normalized_model_config_ids:
        return

    result = await db.execute(
        select(ModelConfig.id).where(
            ModelConfig.profile_id == profile_id,
            ModelConfig.id.in_(normalized_model_config_ids),
        )
    )
    existing_ids = set(result.scalars().all())
    missing_ids = [
        model_config_id
        for model_config_id in normalized_model_config_ids
        if model_config_id not in existing_ids
    ]
    if missing_ids:
        raise HTTPException(status_code=404, detail="Model configuration not found")


async def _list_ordered_connections(
    db: AsyncSession,
    *,
    profile_id: int,
    model_config_id: int,
) -> list[Connection]:
    result = await db.execute(
        select(Connection)
        .options(
            selectinload(Connection.endpoint_rel),
            selectinload(Connection.pricing_template_rel),
        )
        .where(
            Connection.model_config_id == model_config_id,
            Connection.profile_id == profile_id,
        )
        .order_by(Connection.priority.asc(), Connection.id.asc())
    )
    return list(result.scalars().all())


async def _list_ordered_connections_for_models(
    db: AsyncSession,
    *,
    profile_id: int,
    model_config_ids: list[int],
) -> dict[int, list[Connection]]:
    normalized_model_config_ids = list(dict.fromkeys(model_config_ids))
    connections_by_model: dict[int, list[Connection]] = {
        model_config_id: [] for model_config_id in normalized_model_config_ids
    }
    if not normalized_model_config_ids:
        return connections_by_model

    result = await db.execute(
        select(Connection)
        .options(
            selectinload(Connection.endpoint_rel),
            selectinload(Connection.pricing_template_rel),
        )
        .where(
            Connection.model_config_id.in_(normalized_model_config_ids),
            Connection.profile_id == profile_id,
        )
        .order_by(
            Connection.model_config_id.asc(),
            Connection.priority.asc(),
            Connection.id.asc(),
        )
    )

    for connection in result.scalars().all():
        connections_by_model.setdefault(connection.model_config_id, []).append(
            connection
        )

    return connections_by_model


def _normalize_connection_priorities(connections: list[Connection]) -> None:
    normalize_ordered_field(list(connections), field_name="priority")


def _serialize_custom_headers(custom_headers: dict[str, str] | None) -> str | None:
    return json.dumps(custom_headers) if custom_headers else None


__all__ = [
    "_create_endpoint_from_inline",
    "_ensure_model_config_ids_exist",
    "_list_ordered_connections",
    "_list_ordered_connections_for_models",
    "_load_connection_or_404",
    "_load_model_or_404",
    "_lock_profile_row",
    "_normalize_connection_priorities",
    "_serialize_custom_headers",
    "_validate_pricing_template_id",
]
