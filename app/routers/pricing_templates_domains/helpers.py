from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.models import Connection, PricingTemplate
from app.schemas.schemas import PricingTemplateConnectionUsageItem

PRICING_AFFECTING_FIELDS = {
    "pricing_unit",
    "pricing_currency_code",
    "input_price",
    "output_price",
    "cached_input_price",
    "cache_creation_price",
    "reasoning_price",
    "missing_special_token_price_policy",
}


async def load_template_or_404(
    db: AsyncSession,
    *,
    profile_id: int,
    template_id: int,
    lock_for_update: bool = False,
) -> PricingTemplate:
    query = select(PricingTemplate).where(
        PricingTemplate.id == template_id,
        PricingTemplate.profile_id == profile_id,
    )
    if lock_for_update:
        query = query.with_for_update()
    result = await db.execute(query)
    template = result.scalar_one_or_none()
    if template is None:
        raise HTTPException(status_code=404, detail="Pricing template not found")
    return template


async def ensure_unique_template_name(
    db: AsyncSession,
    *,
    profile_id: int,
    name: str,
    exclude_id: int | None = None,
) -> None:
    query = select(PricingTemplate).where(
        PricingTemplate.profile_id == profile_id,
        PricingTemplate.name == name,
    )
    if exclude_id is not None:
        query = query.where(PricingTemplate.id != exclude_id)
    existing = (await db.execute(query)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Pricing template name '{name}' already exists",
        )


async def list_template_connection_rows(
    db: AsyncSession,
    *,
    profile_id: int,
    template_id: int,
) -> list[Connection]:
    return list(
        (
            await db.execute(
                select(Connection)
                .options(
                    selectinload(Connection.model_config_rel),
                    selectinload(Connection.endpoint_rel),
                )
                .where(
                    Connection.profile_id == profile_id,
                    Connection.pricing_template_id == template_id,
                )
                .order_by(Connection.id.asc())
            )
        )
        .scalars()
        .all()
    )


def build_connection_usage_item(
    connection: Connection,
) -> PricingTemplateConnectionUsageItem:
    return PricingTemplateConnectionUsageItem(
        connection_id=connection.id,
        connection_name=connection.name,
        model_config_id=connection.model_config_id,
        model_id=(
            connection.model_config_rel.model_id
            if connection.model_config_rel is not None
            else ""
        ),
        endpoint_id=connection.endpoint_id,
        endpoint_name=(
            connection.endpoint_rel.name if connection.endpoint_rel is not None else ""
        ),
    )


def build_connection_usage_detail(connection: Connection) -> dict[str, object | None]:
    return {
        "connection_id": connection.id,
        "connection_name": connection.name,
        "model_config_id": connection.model_config_id,
        "model_id": (
            connection.model_config_rel.model_id
            if connection.model_config_rel is not None
            else None
        ),
        "endpoint_id": connection.endpoint_id,
        "endpoint_name": (
            connection.endpoint_rel.name
            if connection.endpoint_rel is not None
            else None
        ),
    }


__all__ = [
    "PRICING_AFFECTING_FIELDS",
    "build_connection_usage_detail",
    "build_connection_usage_item",
    "ensure_unique_template_name",
    "list_template_connection_rows",
    "load_template_or_404",
]
