# ruff: noqa: F401
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_active_profile_id
from app.services.loadbalancer import (
    build_attempt_plan,
    clear_current_state,
    get_model_config_with_connections,
    mark_connection_failed,
    mark_connection_recovered,
)
from app.services.proxy_service import (
    build_upstream_headers,
    build_upstream_url,
    filter_response_headers,
    proxy_request,
    should_failover,
)
from app.services.costing_service import compute_cost_fields, load_costing_settings
from app.services.stats_service import log_request
from app.services.audit_service import record_audit_log
from app.routers.proxy_domains.attempt_execution import (
    ProxyRuntimeDependencies,
    execute_proxy_attempts,
)
from app.routers.proxy_domains.request_setup import prepare_proxy_request
from app.routers.proxy_domains.proxy_request_helpers import (
    _classify_failover_failure,
    _classify_http_failure,
    _endpoint_is_active_now,
    _extract_model_from_path,
    _extract_error_text,
    _is_recovery_success_status,
    _resolve_model_id,
    _rewrite_model_in_body,
    _rewrite_model_in_path,
    _track_detached_task,
    _validate_provider_path_compatibility,
)

router = APIRouter(tags=["proxy"])


async def _handle_proxy(
    request: Request,
    db: AsyncSession,
    raw_body: bytes | None,
    request_path: str,
    profile_id: int,
):
    setup = await prepare_proxy_request(
        build_attempt_plan_fn=build_attempt_plan,
        compute_cost_fields_fn=compute_cost_fields,
        get_model_config_with_connections_fn=get_model_config_with_connections,
        load_costing_settings_fn=load_costing_settings,
        request=request,
        db=db,
        raw_body=raw_body,
        request_path=request_path,
        profile_id=profile_id,
    )
    return await execute_proxy_attempts(
        db=db,
        endpoint_is_active_now_fn=_endpoint_is_active_now,
        request_path=request_path,
        request_query=request.url.query or None,
        profile_id=profile_id,
        setup=setup,
        deps=ProxyRuntimeDependencies(
            build_upstream_headers_fn=build_upstream_headers,
            build_upstream_url_fn=build_upstream_url,
            clear_current_state_fn=clear_current_state,
            filter_response_headers_fn=filter_response_headers,
            log_request_fn=log_request,
            mark_connection_failed_fn=mark_connection_failed,
            mark_connection_recovered_fn=mark_connection_recovered,
            proxy_request_fn=proxy_request,
            record_audit_log_fn=record_audit_log,
            should_failover_fn=should_failover,
        ),
    )


@router.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_catch_all(
    request: Request,
    path: str,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    profile_id: Annotated[int, Depends(get_active_profile_id)],
):
    raw_body = await request.body() or None
    return await _handle_proxy(request, db, raw_body, f"/v1/{path}", profile_id)


@router.api_route(
    "/v1beta/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
)
async def proxy_catch_all_v1beta(
    request: Request,
    path: str,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    profile_id: Annotated[int, Depends(get_active_profile_id)],
):
    raw_body = await request.body() or None
    return await _handle_proxy(request, db, raw_body, f"/v1beta/{path}", profile_id)
