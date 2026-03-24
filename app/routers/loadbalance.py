from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import ensure_utc_datetime
from app.dependencies import get_db, get_effective_profile_id
from app.schemas.schemas import (
    LoadbalanceCurrentStateListResponse,
    LoadbalanceCurrentStateResetResponse,
    LoadbalanceStrategyCreate,
    LoadbalanceStrategyResponse,
    LoadbalanceStrategyUpdate,
    LoadbalanceEventDeleteResponse,
    LoadbalanceEventDetail,
    LoadbalanceEventListResponse,
)
from app.services.loadbalance_cleanup import delete_loadbalance_events_in_background
from app.services.loadbalancer.admin import (
    get_event_detail,
    list_model_current_state,
    list_model_events,
    reset_connection_current_state,
)
from app.services.loadbalancer.strategies import (
    create_loadbalance_strategy,
    delete_loadbalance_strategy,
    get_loadbalance_strategy,
    list_loadbalance_strategies,
    update_loadbalance_strategy,
)

router = APIRouter(prefix="/api/loadbalance", tags=["loadbalance"])


@router.get("/strategies", response_model=list[LoadbalanceStrategyResponse])
async def list_strategies(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await list_loadbalance_strategies(db, profile_id=profile_id)


@router.post("/strategies", response_model=LoadbalanceStrategyResponse, status_code=201)
async def create_strategy(
    body: LoadbalanceStrategyCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await create_loadbalance_strategy(
        db,
        profile_id=profile_id,
        body=body,
    )


@router.get("/strategies/{strategy_id}", response_model=LoadbalanceStrategyResponse)
async def get_strategy(
    strategy_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await get_loadbalance_strategy(
        db,
        profile_id=profile_id,
        strategy_id=strategy_id,
    )


@router.put("/strategies/{strategy_id}", response_model=LoadbalanceStrategyResponse)
async def update_strategy(
    strategy_id: int,
    body: LoadbalanceStrategyUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await update_loadbalance_strategy(
        db,
        profile_id=profile_id,
        strategy_id=strategy_id,
        body=body,
    )


@router.delete("/strategies/{strategy_id}")
async def delete_strategy(
    strategy_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await delete_loadbalance_strategy(
        db,
        profile_id=profile_id,
        strategy_id=strategy_id,
    )


@router.get("/current-state", response_model=LoadbalanceCurrentStateListResponse)
async def list_loadbalance_current_state(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    model_config_id: Annotated[int, Query(ge=1)],
):
    return await list_model_current_state(
        db=db,
        profile_id=profile_id,
        model_config_id=model_config_id,
    )


@router.post(
    "/current-state/{connection_id}/reset",
    response_model=LoadbalanceCurrentStateResetResponse,
)
async def reset_loadbalance_current_state(
    connection_id: int,
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await reset_connection_current_state(
        profile_id=profile_id,
        connection_id=connection_id,
    )


@router.get("/events", response_model=LoadbalanceEventListResponse)
async def list_loadbalance_events(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    model_id: Annotated[str, Query(min_length=1)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return await list_model_events(
        db=db,
        profile_id=profile_id,
        model_id=model_id,
        limit=limit,
        offset=offset,
    )


@router.get("/events/{event_id}", response_model=LoadbalanceEventDetail)
async def get_loadbalance_event(
    event_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await get_event_detail(
        db=db,
        profile_id=profile_id,
        event_id=event_id,
    )


@router.delete("/events", response_model=LoadbalanceEventDeleteResponse)
async def delete_loadbalance_events(
    background_tasks: BackgroundTasks,
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    before: datetime | None = None,
    older_than_days: Annotated[int | None, Query(ge=1)] = None,
    delete_all: Annotated[bool, Query()] = False,
):
    normalized_before = ensure_utc_datetime(before)

    provided = sum([before is not None, older_than_days is not None, delete_all])
    if provided != 1:
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of 'before', 'older_than_days', or 'delete_all'",
        )

    if before is not None and normalized_before is None:
        raise HTTPException(status_code=400, detail="'before' is required")

    background_tasks.add_task(
        delete_loadbalance_events_in_background,
        profile_id=profile_id,
        before=normalized_before,
        older_than_days=older_than_days,
        delete_all=delete_all,
    )
    return LoadbalanceEventDeleteResponse(accepted=True)
