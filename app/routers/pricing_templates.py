from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.time import utc_now
from app.dependencies import get_db, get_effective_profile_id
from app.models.models import Connection, ModelConfig, PricingTemplate
from app.schemas.schemas import (
    PricingTemplateConnectionsResponse,
    PricingTemplateConnectionUsageItem,
    PricingTemplateCreate,
    PricingTemplateListItem,
    PricingTemplateResponse,
    PricingTemplateUpdate,
)

router = APIRouter(prefix="/api/pricing-templates", tags=["pricing-templates"] )

_PRICING_AFFECTING_FIELDS = {
    "pricing_unit",
    "pricing_currency_code",
    "input_price",
    "output_price",
    "cached_input_price",
    "cache_creation_price",
    "reasoning_price",
    "missing_special_token_price_policy",
}


async def _load_template_or_404(
    db: AsyncSession,
    *,
    profile_id: int,
    template_id: int,
 ) -> PricingTemplate:
    result = await db.execute(
        select(PricingTemplate).where(
            PricingTemplate.id == template_id,
            PricingTemplate.profile_id == profile_id,
        )
    )
    template = result.scalar_one_or_none()
    if template is None:
        raise HTTPException(status_code=404, detail="Pricing template not found")
    return template


async def _ensure_unique_template_name(
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


@router.get("", response_model=list[PricingTemplateListItem])
async def list_pricing_templates(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
 ):
    result = await db.execute(
        select(PricingTemplate)
        .where(PricingTemplate.profile_id == profile_id)
        .order_by(PricingTemplate.updated_at.desc(), PricingTemplate.id.desc())
    )
    return result.scalars().all()


@router.post("", response_model=PricingTemplateResponse, status_code=201)
async def create_pricing_template(
    body: PricingTemplateCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
 ):
    await _ensure_unique_template_name(
        db,
        profile_id=profile_id,
        name=body.name,
    )

    template = PricingTemplate(
        profile_id=profile_id,
        name=body.name,
        description=body.description,
        pricing_unit=body.pricing_unit,
        pricing_currency_code=body.pricing_currency_code,
        input_price=body.input_price,
        output_price=body.output_price,
        cached_input_price=body.cached_input_price,
        cache_creation_price=body.cache_creation_price,
        reasoning_price=body.reasoning_price,
        missing_special_token_price_policy=body.missing_special_token_price_policy,
        version=1,
    )
    db.add(template)
    await db.flush()
    await db.refresh(template)
    return template


@router.get("/{template_id}", response_model=PricingTemplateResponse)
async def get_pricing_template(
    template_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
 ):
    return await _load_template_or_404(
        db,
        profile_id=profile_id,
        template_id=template_id,
    )


@router.put("/{template_id}", response_model=PricingTemplateResponse)
async def update_pricing_template(
    template_id: int,
    body: PricingTemplateUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
 ):
    template = await _load_template_or_404(
        db,
        profile_id=profile_id,
        template_id=template_id,
    )

    update_data = body.model_dump(exclude_unset=True)
    if "name" in update_data:
        await _ensure_unique_template_name(
            db,
            profile_id=profile_id,
            name=update_data["name"],
            exclude_id=template.id,
        )

    pricing_changed = any(
        field_name in update_data
        and update_data[field_name] != getattr(template, field_name)
        for field_name in _PRICING_AFFECTING_FIELDS
    )

    for key, value in update_data.items():
        setattr(template, key, value)

    if pricing_changed:
        template.version = (template.version or 1) + 1
    template.updated_at = utc_now()
    await db.flush()
    await db.refresh(template)
    return template


@router.delete("/{template_id}")
async def delete_pricing_template(
    template_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
 ):
    template = await _load_template_or_404(
        db,
        profile_id=profile_id,
        template_id=template_id,
    )

    usage_rows = (
        (
            await db.execute(
                select(Connection)
                .options(
                    selectinload(Connection.model_config_rel),
                    selectinload(Connection.endpoint_rel),
                )
                .where(
                    Connection.profile_id == profile_id,
                    Connection.pricing_template_id == template.id,
                )
                .order_by(Connection.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if usage_rows:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Cannot delete pricing template that is referenced by connections",
                "connections": [
                    {
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
                    for connection in usage_rows
                ],
            },
        )

    await db.delete(template)
    await db.flush()
    return {"deleted": True}


@router.get("/{template_id}/connections", response_model=PricingTemplateConnectionsResponse)
async def get_pricing_template_connections(
    template_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
 ):
    template = await _load_template_or_404(
        db,
        profile_id=profile_id,
        template_id=template_id,
    )
    rows = (
        (
            await db.execute(
                select(Connection)
                .options(
                    selectinload(Connection.model_config_rel),
                    selectinload(Connection.endpoint_rel),
                )
                .where(
                    Connection.profile_id == profile_id,
                    Connection.pricing_template_id == template.id,
                )
                .order_by(Connection.id.asc())
            )
        )
        .scalars()
        .all()
    )

    return PricingTemplateConnectionsResponse(
        template_id=template.id,
        items=[
            PricingTemplateConnectionUsageItem(
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
                    connection.endpoint_rel.name
                    if connection.endpoint_rel is not None
                    else ""
                ),
            )
            for connection in rows
        ],
    )