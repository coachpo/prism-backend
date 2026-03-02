from typing import Annotated, Literal, cast
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_db, get_effective_profile_id
from app.models.models import EndpointFxRateSetting, ModelConfig, Provider
from app.schemas.schemas import (
    ModelConfigCreate,
    ModelConfigUpdate,
    ModelConfigResponse,
    ModelConfigListResponse,
    ProviderResponse,
)
from app.services.stats_service import get_model_health_stats

router = APIRouter(prefix="/api/models", tags=["models"])

@router.get("", response_model=list[ModelConfigListResponse])
async def list_models(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    result = await db.execute(
        select(ModelConfig)
        .options(
            selectinload(ModelConfig.provider), selectinload(ModelConfig.connections)
        )
        .where(
            ModelConfig.profile_id == profile_id,
            ModelConfig.model_type == "native",
        )
        .order_by(ModelConfig.id)
    )
    configs = result.scalars().all()

    health_stats = await get_model_health_stats(db, profile_id=profile_id)

    response = []
    for config in configs:
        stats = health_stats.get(config.model_id, {})
        response.append(
            ModelConfigListResponse(
                id=config.id,
                profile_id=config.profile_id,
                provider_id=config.provider_id,
                provider=ProviderResponse.model_validate(config.provider),
                model_id=config.model_id,
                display_name=config.display_name,
                model_type=config.model_type,
                lb_strategy=cast(
                    Literal["single", "failover"],
                    "failover" if config.lb_strategy == "failover" else "single",
                ),
                failover_recovery_enabled=config.failover_recovery_enabled,
                failover_recovery_cooldown_seconds=config.failover_recovery_cooldown_seconds,
                is_enabled=config.is_enabled,
                connection_count=len(config.connections),
                active_connection_count=sum(
                    1 for connection in config.connections if connection.is_active
                ),
                health_success_rate=stats.get("health_success_rate"),
                health_total_requests=stats.get("health_total_requests", 0),
                created_at=config.created_at,
                updated_at=config.updated_at,
            )
        )
    return response


@router.get("/{model_config_id}", response_model=ModelConfigResponse)
async def get_model(
    model_config_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    result = await db.execute(
        select(ModelConfig)
        .options(
            selectinload(ModelConfig.provider), selectinload(ModelConfig.connections)
        )
        .where(
            ModelConfig.id == model_config_id,
            ModelConfig.profile_id == profile_id,
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Model configuration not found")
    if config.model_type != "native":
        raise HTTPException(status_code=404, detail="Model configuration not found")
    return config


@router.post("", response_model=ModelConfigResponse, status_code=201)
async def create_model(
    body: ModelConfigCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    provider = await db.get(Provider, body.provider_id)
    if not provider:
        raise HTTPException(status_code=400, detail="Provider not found")

    existing = await db.execute(
        select(ModelConfig).where(
            ModelConfig.profile_id == profile_id,
            ModelConfig.model_id == body.model_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail=f"Model ID '{body.model_id}' already exists"
        )

    config = ModelConfig(
        profile_id=profile_id,
        provider_id=body.provider_id,
        model_id=body.model_id,
        display_name=body.display_name,
        model_type="native",
        redirect_to=None,
        lb_strategy=body.lb_strategy,
        failover_recovery_enabled=body.failover_recovery_enabled,
        failover_recovery_cooldown_seconds=body.failover_recovery_cooldown_seconds,
        is_enabled=body.is_enabled,
    )
    db.add(config)
    await db.flush()

    result = await db.execute(
        select(ModelConfig)
        .options(
            selectinload(ModelConfig.provider), selectinload(ModelConfig.connections)
        )
        .where(ModelConfig.id == config.id)
    )
    return result.scalar_one()


@router.put("/{model_config_id}", response_model=ModelConfigResponse)
async def update_model(
    model_config_id: int,
    body: ModelConfigUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    result = await db.execute(
        select(ModelConfig)
        .options(
            selectinload(ModelConfig.provider), selectinload(ModelConfig.connections)
        )
        .where(
            ModelConfig.id == model_config_id,
            ModelConfig.profile_id == profile_id,
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Model configuration not found")
    if config.model_type != "native":
        raise HTTPException(status_code=404, detail="Model configuration not found")

    original_model_id = config.model_id
    update_data = body.model_dump(exclude_unset=True)

    if "provider_id" in update_data:
        provider = await db.get(Provider, update_data["provider_id"])
        if not provider:
            raise HTTPException(status_code=400, detail="Provider not found")

    if "model_id" in update_data and update_data["model_id"] != config.model_id:
        existing = await db.execute(
            select(ModelConfig).where(
                ModelConfig.profile_id == profile_id,
                ModelConfig.model_id == update_data["model_id"],
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail=f"Model ID '{update_data['model_id']}' already exists",
            )

    update_data["model_type"] = "native"
    update_data["redirect_to"] = None

    for key, value in update_data.items():
        setattr(config, key, value)

    if "model_id" in update_data and update_data["model_id"] != original_model_id:
        await db.execute(
            update(EndpointFxRateSetting)
            .where(
                EndpointFxRateSetting.profile_id == profile_id,
                EndpointFxRateSetting.model_id == original_model_id,
            )
            .values(model_id=update_data["model_id"])
        )

    config.updated_at = datetime.utcnow()
    await db.flush()

    result = await db.execute(
        select(ModelConfig)
        .options(
            selectinload(ModelConfig.provider), selectinload(ModelConfig.connections)
        )
        .where(ModelConfig.id == config.id)
    )
    return result.scalar_one()


@router.delete("/{model_config_id}")
async def delete_model(
    model_config_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    result = await db.execute(
        select(ModelConfig).where(
            ModelConfig.id == model_config_id,
            ModelConfig.profile_id == profile_id,
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Model configuration not found")
    if config.model_type != "native":
        raise HTTPException(status_code=404, detail="Model configuration not found")

    await db.delete(config)
    await db.flush()
    return {"deleted": True}


@router.get("/by-endpoint/{endpoint_id}", response_model=list[ModelConfigListResponse])
async def get_models_by_endpoint(
    endpoint_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    """Get all models that use a specific endpoint."""
    from app.models.models import Connection
    
    # Find all connections using this endpoint
    result = await db.execute(
        select(Connection)
        .options(selectinload(Connection.model_config_rel).selectinload(ModelConfig.provider))
        .where(
            Connection.endpoint_id == endpoint_id,
            Connection.profile_id == profile_id,
        )
    )
    connections = result.scalars().all()
    
    # Extract unique model configs
    model_configs = {conn.model_config_rel for conn in connections}
    
    # Get health stats
    health_stats = await get_model_health_stats(db, profile_id=profile_id)
    
    # Build response
    response = []
    for config in model_configs:
        stats = health_stats.get(config.model_id, {})
        
        # Count connections for this model
        model_connections = [c for c in connections if c.model_config_id == config.id]
        active_count = sum(1 for c in model_connections if c.is_active)
        
        response.append(
            ModelConfigListResponse(
                id=config.id,
                profile_id=config.profile_id,
                provider_id=config.provider_id,
                provider=ProviderResponse.model_validate(config.provider),
                model_id=config.model_id,
                display_name=config.display_name,
                model_type=config.model_type,
                lb_strategy=cast(
                    Literal["single", "failover"],
                    "failover" if config.lb_strategy == "failover" else "single",
                ),
                failover_recovery_enabled=config.failover_recovery_enabled,
                failover_recovery_cooldown_seconds=config.failover_recovery_cooldown_seconds,
                is_enabled=config.is_enabled,
                connection_count=len(model_connections),
                active_connection_count=active_count,
                health_success_rate=stats.get("health_success_rate"),
                health_total_requests=stats.get("health_total_requests", 0),
                created_at=config.created_at,
                updated_at=config.updated_at,
            )
        )
    
    return sorted(response, key=lambda m: m.model_id)
