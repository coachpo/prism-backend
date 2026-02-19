from typing import Annotated
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_db
from app.models.models import ModelConfig, Provider
from app.schemas.schemas import (
    ModelConfigCreate,
    ModelConfigUpdate,
    ModelConfigResponse,
    ModelConfigListResponse,
    ProviderResponse,
)

router = APIRouter(prefix="/api/models", tags=["models"])


async def _validate_redirect(
    db: AsyncSession,
    model_type: str,
    redirect_to: str | None,
    provider_id: int,
    exclude_model_id: str | None = None,
):
    if model_type == "redirect":
        if not redirect_to:
            raise HTTPException(
                status_code=400,
                detail="redirect_to is required for redirect models",
            )
        target_result = await db.execute(
            select(ModelConfig)
            .options(selectinload(ModelConfig.provider))
            .where(ModelConfig.model_id == redirect_to)
        )
        target = target_result.scalar_one_or_none()
        if not target:
            raise HTTPException(
                status_code=400,
                detail=f"Target model '{redirect_to}' not found",
            )
        if target.model_type != "native":
            raise HTTPException(
                status_code=400,
                detail=f"Target model '{redirect_to}' is not a native model (chained redirects not allowed)",
            )
        if target.provider_id != provider_id:
            raise HTTPException(
                status_code=400,
                detail="Redirect target must be the same provider as the redirect model",
            )
    elif model_type == "native":
        if redirect_to:
            raise HTTPException(
                status_code=400,
                detail="redirect_to must be null for native models",
            )


@router.get("", response_model=list[ModelConfigListResponse])
async def list_models(db: Annotated[AsyncSession, Depends(get_db)]):
    result = await db.execute(
        select(ModelConfig)
        .options(
            selectinload(ModelConfig.provider), selectinload(ModelConfig.endpoints)
        )
        .order_by(ModelConfig.id)
    )
    configs = result.scalars().all()

    response = []
    for config in configs:
        response.append(
            ModelConfigListResponse(
                id=config.id,
                provider_id=config.provider_id,
                provider=ProviderResponse.model_validate(config.provider),
                model_id=config.model_id,
                display_name=config.display_name,
                model_type=config.model_type,
                redirect_to=config.redirect_to,
                lb_strategy=config.lb_strategy,
                is_enabled=config.is_enabled,
                endpoint_count=len(config.endpoints),
                active_endpoint_count=sum(1 for ep in config.endpoints if ep.is_active),
                created_at=config.created_at,
                updated_at=config.updated_at,
            )
        )
    return response


@router.get("/{model_config_id}", response_model=ModelConfigResponse)
async def get_model(model_config_id: int, db: Annotated[AsyncSession, Depends(get_db)]):
    result = await db.execute(
        select(ModelConfig)
        .options(
            selectinload(ModelConfig.provider), selectinload(ModelConfig.endpoints)
        )
        .where(ModelConfig.id == model_config_id)
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Model configuration not found")
    return config


@router.post("", response_model=ModelConfigResponse, status_code=201)
async def create_model(
    body: ModelConfigCreate, db: Annotated[AsyncSession, Depends(get_db)]
):
    provider = await db.get(Provider, body.provider_id)
    if not provider:
        raise HTTPException(status_code=400, detail="Provider not found")

    existing = await db.execute(
        select(ModelConfig).where(ModelConfig.model_id == body.model_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail=f"Model ID '{body.model_id}' already exists"
        )

    model_type = body.model_type or "native"
    if model_type not in ("native", "redirect"):
        raise HTTPException(
            status_code=400, detail="model_type must be 'native' or 'redirect'"
        )

    await _validate_redirect(db, model_type, body.redirect_to, body.provider_id)

    config = ModelConfig(
        provider_id=body.provider_id,
        model_id=body.model_id,
        display_name=body.display_name,
        model_type=model_type,
        redirect_to=body.redirect_to if model_type == "redirect" else None,
        lb_strategy=body.lb_strategy if model_type == "native" else "single",
        is_enabled=body.is_enabled,
    )
    db.add(config)
    await db.flush()

    result = await db.execute(
        select(ModelConfig)
        .options(
            selectinload(ModelConfig.provider), selectinload(ModelConfig.endpoints)
        )
        .where(ModelConfig.id == config.id)
    )
    return result.scalar_one()


@router.put("/{model_config_id}", response_model=ModelConfigResponse)
async def update_model(
    model_config_id: int,
    body: ModelConfigUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(ModelConfig)
        .options(
            selectinload(ModelConfig.provider), selectinload(ModelConfig.endpoints)
        )
        .where(ModelConfig.id == model_config_id)
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Model configuration not found")

    update_data = body.model_dump(exclude_unset=True)

    if "provider_id" in update_data:
        provider = await db.get(Provider, update_data["provider_id"])
        if not provider:
            raise HTTPException(status_code=400, detail="Provider not found")

    if "model_id" in update_data and update_data["model_id"] != config.model_id:
        existing = await db.execute(
            select(ModelConfig).where(ModelConfig.model_id == update_data["model_id"])
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail=f"Model ID '{update_data['model_id']}' already exists",
            )

    new_model_type = update_data.get("model_type", config.model_type)
    new_redirect_to = update_data.get("redirect_to", config.redirect_to)
    new_provider_id = update_data.get("provider_id", config.provider_id)

    if new_model_type not in ("native", "redirect"):
        raise HTTPException(
            status_code=400, detail="model_type must be 'native' or 'redirect'"
        )

    await _validate_redirect(
        db,
        new_model_type,
        new_redirect_to,
        new_provider_id,
        exclude_model_id=config.model_id,
    )

    if new_model_type == "native":
        update_data["redirect_to"] = None

    for key, value in update_data.items():
        setattr(config, key, value)
    config.updated_at = datetime.utcnow()
    await db.flush()

    result = await db.execute(
        select(ModelConfig)
        .options(
            selectinload(ModelConfig.provider), selectinload(ModelConfig.endpoints)
        )
        .where(ModelConfig.id == config.id)
    )
    return result.scalar_one()


@router.delete("/{model_config_id}", status_code=204)
async def delete_model(
    model_config_id: int, db: Annotated[AsyncSession, Depends(get_db)]
):
    result = await db.execute(
        select(ModelConfig).where(ModelConfig.id == model_config_id)
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Model configuration not found")

    if config.model_type == "native":
        referrers = await db.execute(
            select(ModelConfig).where(ModelConfig.redirect_to == config.model_id)
        )
        referrer_list = referrers.scalars().all()
        if referrer_list:
            ids = ", ".join(r.model_id for r in referrer_list)
            raise HTTPException(
                status_code=400,
                detail=f"Cannot delete: redirect models [{ids}] point to this model",
            )

    await db.delete(config)
