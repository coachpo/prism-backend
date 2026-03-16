from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.dependencies import get_db, get_effective_profile_id
from app.models.models import PricingTemplate
from app.schemas.schemas import (
    PricingTemplateConnectionUsageItem,
    PricingTemplateConnectionsResponse,
    PricingTemplateCreate,
    PricingTemplateResponse,
    PricingTemplateUpdate,
)

from .helpers import (
    PRICING_AFFECTING_FIELDS,
    build_connection_usage_detail,
    build_connection_usage_item,
    ensure_unique_template_name,
    list_template_connection_rows,
    load_template_or_404,
)

router = APIRouter(prefix="/api/pricing-templates", tags=["pricing-templates"])


@router.get("", response_model=list[PricingTemplateResponse])
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
    await ensure_unique_template_name(
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
    return await load_template_or_404(
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
    template = await load_template_or_404(
        db,
        profile_id=profile_id,
        template_id=template_id,
        lock_for_update=True,
    )

    update_data = body.model_dump(exclude_unset=True)
    expected_updated_at = update_data.pop("expected_updated_at")
    if template.updated_at != expected_updated_at:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Pricing template has changed. Refresh and retry your edit.",
                "current_version": template.version,
                "current_updated_at": template.updated_at.isoformat(),
            },
        )

    if "name" in update_data:
        await ensure_unique_template_name(
            db,
            profile_id=profile_id,
            name=update_data["name"],
            exclude_id=template.id,
        )

    changed_fields = {
        key: value
        for key, value in update_data.items()
        if value != getattr(template, key)
    }
    pricing_changed = any(
        field_name in changed_fields for field_name in PRICING_AFFECTING_FIELDS
    )

    for key, value in changed_fields.items():
        setattr(template, key, value)

    if changed_fields:
        template.updated_at = utc_now()
    if pricing_changed:
        template.version = (template.version or 1) + 1
    await db.flush()
    await db.refresh(template)
    return template


@router.delete("/{template_id}")
async def delete_pricing_template(
    template_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    template = await load_template_or_404(
        db,
        profile_id=profile_id,
        template_id=template_id,
    )
    usage_rows = await list_template_connection_rows(
        db,
        profile_id=profile_id,
        template_id=template.id,
    )
    if usage_rows:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Cannot delete pricing template that is referenced by connections",
                "connections": [
                    build_connection_usage_detail(connection)
                    for connection in usage_rows
                ],
            },
        )

    await db.delete(template)
    await db.flush()
    return {"deleted": True}


@router.get(
    "/{template_id}/connections", response_model=PricingTemplateConnectionsResponse
)
async def get_pricing_template_connections(
    template_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    template = await load_template_or_404(
        db,
        profile_id=profile_id,
        template_id=template_id,
    )
    rows = await list_template_connection_rows(
        db,
        profile_id=profile_id,
        template_id=template.id,
    )

    return PricingTemplateConnectionsResponse(
        template_id=template.id,
        items=[
            PricingTemplateConnectionUsageItem.model_validate(
                build_connection_usage_item(connection)
            )
            for connection in rows
        ],
    )


__all__ = [
    "create_pricing_template",
    "delete_pricing_template",
    "get_pricing_template",
    "get_pricing_template_connections",
    "list_pricing_templates",
    "router",
    "update_pricing_template",
]
