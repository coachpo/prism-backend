# ruff: noqa: F401
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_effective_profile_id
from app.schemas.schemas import (
    ConnectionCreate,
    ModelConnectionsBatchItem,
    ModelConnectionsBatchRequest,
    ModelConnectionsBatchResponse,
    ConnectionOwnerResponse,
    ConnectionPriorityMoveRequest,
    ConnectionPricingTemplateUpdate,
    ConnectionResponse,
    ConnectionUpdate,
    HealthCheckResponse,
)
from app.services.loadbalancer.recovery import record_connection_recovery
from app.services.loadbalancer.state import (
    clear_connection_state,
    clear_round_robin_state_for_model,
)
from app.services.proxy_service import (
    build_upstream_headers,
)
from app.routers.connections_domains.connection_crud_helpers import (
    _create_endpoint_from_inline,
    _ensure_model_config_ids_exist,
    _list_ordered_connections,
    _list_ordered_connections_for_models,
    _load_connection_or_404,
    _load_model_or_404,
    _lock_profile_row,
    _normalize_connection_priorities,
    _serialize_custom_headers,
    _validate_pricing_template_id,
)
from app.routers.connections_domains.health_check_builders import (
    _build_health_check_request,
    _build_openai_chat_completions_health_check_request,
    _build_openai_responses_basic_health_check_request,
    _probe_connection_health as _probe_connection_health_impl,
)
from app.routers.connections_domains.health_check_request_helpers import (
    _execute_health_check_request,
)
from app.routers.connections_domains.route_handlers import (
    ConnectionCrudDependencies,
    create_connection_record,
    delete_connection_record,
    get_connection_owner_details,
    list_connections_for_model,
    list_connections_for_models,
    move_connection_priority_for_model,
    perform_connection_health_check,
    set_connection_pricing_template_record,
    update_connection_record,
)

router = APIRouter(tags=["connections"])


def _crud_deps() -> ConnectionCrudDependencies:
    return ConnectionCrudDependencies(
        clear_connection_state_fn=clear_connection_state,
        clear_round_robin_state_for_model_fn=clear_round_robin_state_for_model,
        create_endpoint_from_inline_fn=_create_endpoint_from_inline,
        ensure_model_config_ids_exist_fn=_ensure_model_config_ids_exist,
        list_ordered_connections_fn=_list_ordered_connections,
        list_ordered_connections_for_models_fn=_list_ordered_connections_for_models,
        load_connection_or_404_fn=_load_connection_or_404,
        load_model_or_404_fn=_load_model_or_404,
        lock_profile_row_fn=_lock_profile_row,
        normalize_connection_priorities_fn=_normalize_connection_priorities,
        serialize_custom_headers_fn=_serialize_custom_headers,
        validate_pricing_template_id_fn=_validate_pricing_template_id,
    )


async def _probe_connection_health(**kwargs):
    return await _probe_connection_health_impl(
        **kwargs,
        execute_health_check_request_fn=_execute_health_check_request,
    )


@router.post(
    "/api/models/connections/batch", response_model=ModelConnectionsBatchResponse
)
async def list_connections_batch(
    body: ModelConnectionsBatchRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    connections_by_model = await list_connections_for_models(
        model_config_ids=body.model_config_ids,
        db=db,
        profile_id=profile_id,
        deps=_crud_deps(),
    )
    return ModelConnectionsBatchResponse(
        items=[
            ModelConnectionsBatchItem(
                model_config_id=model_config_id,
                connections=[
                    ConnectionResponse.model_validate(connection)
                    for connection in connections_by_model.get(model_config_id, [])
                ],
            )
            for model_config_id in body.model_config_ids
        ]
    )


@router.get(
    "/api/models/{model_config_id}/connections", response_model=list[ConnectionResponse]
)
async def list_connections(
    model_config_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await list_connections_for_model(
        model_config_id=model_config_id,
        db=db,
        profile_id=profile_id,
        deps=_crud_deps(),
    )


@router.post(
    "/api/models/{model_config_id}/connections",
    response_model=ConnectionResponse,
    status_code=201,
)
async def create_connection(
    model_config_id: int,
    body: ConnectionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await create_connection_record(
        model_config_id=model_config_id,
        body=body,
        db=db,
        profile_id=profile_id,
        deps=_crud_deps(),
    )


@router.put("/api/connections/{connection_id}", response_model=ConnectionResponse)
async def update_connection(
    connection_id: int,
    body: ConnectionUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await update_connection_record(
        connection_id=connection_id,
        body=body,
        db=db,
        profile_id=profile_id,
        deps=_crud_deps(),
    )


@router.patch(
    "/api/models/{model_config_id}/connections/{connection_id}/priority",
    response_model=list[ConnectionResponse],
)
async def move_connection_priority(
    model_config_id: int,
    connection_id: int,
    body: ConnectionPriorityMoveRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await move_connection_priority_for_model(
        model_config_id=model_config_id,
        connection_id=connection_id,
        body=body,
        db=db,
        profile_id=profile_id,
        deps=_crud_deps(),
    )


@router.put(
    "/api/connections/{connection_id}/pricing-template",
    response_model=ConnectionResponse,
)
async def set_connection_pricing_template(
    connection_id: int,
    body: ConnectionPricingTemplateUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await set_connection_pricing_template_record(
        connection_id=connection_id,
        body=body,
        db=db,
        profile_id=profile_id,
        deps=_crud_deps(),
    )


@router.delete("/api/connections/{connection_id}")
async def delete_connection(
    connection_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await delete_connection_record(
        connection_id=connection_id,
        db=db,
        profile_id=profile_id,
        deps=_crud_deps(),
    )


@router.post(
    "/api/connections/{connection_id}/health-check",
    response_model=HealthCheckResponse,
)
async def health_check_connection(
    connection_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await perform_connection_health_check(
        connection_id=connection_id,
        request=request,
        db=db,
        profile_id=profile_id,
        build_upstream_headers_fn=build_upstream_headers,
        probe_connection_health_fn=_probe_connection_health,
        record_connection_recovery_fn=record_connection_recovery,
    )


@router.get(
    "/api/connections/{connection_id}/owner",
    response_model=ConnectionOwnerResponse,
)
async def get_connection_owner(
    connection_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await get_connection_owner_details(
        connection_id=connection_id,
        db=db,
        profile_id=profile_id,
    )
