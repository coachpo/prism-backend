from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_effective_profile_id
from app.schemas.schemas import (
    ModelConfigCreate,
    ModelConfigListResponse,
    ModelConfigResponse,
    ModelConfigUpdate,
)
from app.services.stats_service import get_model_health_stats
from app.routers.models_domains.handlers import (
    create_model_config_record,
    delete_model_config_record,
    get_model_detail,
    get_models_by_endpoint_for_profile,
    list_models_for_profile,
    update_model_config_record,
)

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("", response_model=list[ModelConfigListResponse])
async def list_models(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await list_models_for_profile(
        db,
        profile_id=profile_id,
        get_model_health_stats_fn=get_model_health_stats,
    )


@router.get("/{model_config_id}", response_model=ModelConfigResponse)
async def get_model(
    model_config_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await get_model_detail(
        db,
        model_config_id=model_config_id,
        profile_id=profile_id,
    )


@router.post("", response_model=ModelConfigResponse, status_code=201)
async def create_model(
    body: ModelConfigCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await create_model_config_record(
        db,
        body=body,
        profile_id=profile_id,
    )


@router.put("/{model_config_id}", response_model=ModelConfigResponse)
async def update_model(
    model_config_id: int,
    body: ModelConfigUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await update_model_config_record(
        db,
        model_config_id=model_config_id,
        body=body,
        profile_id=profile_id,
    )


@router.delete("/{model_config_id}")
async def delete_model(
    model_config_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await delete_model_config_record(
        db,
        model_config_id=model_config_id,
        profile_id=profile_id,
    )


@router.get("/by-endpoint/{endpoint_id}", response_model=list[ModelConfigListResponse])
async def get_models_by_endpoint(
    endpoint_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await get_models_by_endpoint_for_profile(
        db,
        endpoint_id=endpoint_id,
        profile_id=profile_id,
        get_model_health_stats_fn=get_model_health_stats,
    )
