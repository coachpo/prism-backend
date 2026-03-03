import asyncio
import json
import logging
import re
import time
from typing import Annotated, AsyncGenerator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_active_profile_id
from app.models.models import Connection, HeaderBlocklistRule
from app.services.loadbalancer import (
    get_model_config_with_connections,
    build_attempt_plan,
    mark_connection_failed,
    mark_connection_recovered,
)
from app.services.proxy_service import (
    build_upstream_url,
    build_upstream_headers,
    proxy_request,
    should_failover,
    extract_model_from_body,
    extract_stream_flag,
    filter_response_headers,
)
from app.services.stats_service import log_request, extract_token_usage
from app.services.costing_service import (
    CostFieldPayload,
    load_costing_settings,
    compute_cost_fields,
)
from app.services.audit_service import record_audit_log

logger = logging.getLogger(__name__)

router = APIRouter(tags=["proxy"])


# Gemini-style URL pattern: /models/{model_id}:{action}
_GEMINI_MODEL_RE = re.compile(r"/models/([^/:]+)")
_GEMINI_NATIVE_PATH_RE = re.compile(
    r"^/v1(?:beta)?/models/[^/:]+:(?:generateContent|streamGenerateContent)/?$"
 )
_ANTHROPIC_MESSAGES_PATH_RE = re.compile(r"^/v1/messages(?:/count_tokens)?/?$")


def _track_detached_task(task: asyncio.Task[None], *, name: str) -> None:
    def _on_done(done_task: asyncio.Task[None]) -> None:
        try:
            done_task.result()
        except asyncio.CancelledError:
            logger.debug("%s cancelled before completion", name)
        except Exception:
            logger.exception("%s failed", name)

    task.add_done_callback(_on_done)


def _get_client_headers(request: Request) -> dict[str, str]:
    return dict(request.headers)


def _extract_model_from_path(request_path: str) -> str | None:
    match = _GEMINI_MODEL_RE.search(request_path)
    return match.group(1) if match else None


def _rewrite_model_in_path(
    request_path: str, original_model: str, target_model: str
) -> str:
    if original_model == target_model:
        return request_path
    return request_path.replace(
        f"/models/{original_model}", f"/models/{target_model}", 1
    )


async def _endpoint_is_active_now(
    db: AsyncSession, connection_id: int, profile_id: int | None = None
 ) -> bool:
    query = select(Connection.is_active).where(Connection.id == connection_id)
    if profile_id is not None:
        query = query.where(Connection.profile_id == profile_id)
    result = await db.execute(query)
    return bool(result.scalar_one_or_none())


def _resolve_model_id(raw_body: bytes | None, request_path: str) -> str | None:
    if not raw_body:
        return _extract_model_from_path(request_path)
    model_id = extract_model_from_body(raw_body)
    if model_id:
        return model_id
    # Gemini-style requests can carry model in path instead of JSON body.
    return _extract_model_from_path(request_path)


def _classify_request_path(request_path: str) -> str:
    if _GEMINI_NATIVE_PATH_RE.match(request_path):
        return "gemini_native"
    if _ANTHROPIC_MESSAGES_PATH_RE.match(request_path):
        return "anthropic_messages"
    return "generic"


_PROVIDER_PATH_FAMILIES: dict[str, set[str]] = {
    "openai": {"generic"},
    "anthropic": {"anthropic_messages"},
    "gemini": {"generic", "gemini_native"},
}


def _validate_provider_path_compatibility(provider_type: str, request_path: str) -> None:
    allowed_path_families = _PROVIDER_PATH_FAMILIES.get(provider_type)
    if allowed_path_families is None:
        return

    path_family = _classify_request_path(request_path)
    if path_family in allowed_path_families:
        return

    raise HTTPException(
        status_code=400,
        detail=(
            f"Path '{request_path}' is incompatible with provider '{provider_type}'. "
            "Use a provider-native path."
        ),
    )


def _rewrite_model_in_body(raw_body: bytes, target_model_id: str) -> bytes:
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return raw_body
    if not isinstance(payload, dict):
        return raw_body
    payload["model"] = target_model_id
    try:
        return json.dumps(payload).encode("utf-8")
    except (TypeError, ValueError):
        return raw_body


async def _handle_proxy(
    request: Request,
    db: AsyncSession,
    raw_body: bytes | None,
    request_path: str,
    profile_id: int,
 ):
    model_id = _resolve_model_id(raw_body, request_path)
    if not model_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot determine model for routing. "
                "Include 'model' in the request body or use a Gemini-style model path."
            ),
        )
    model_config = await get_model_config_with_connections(db, profile_id, model_id)
    if not model_config:
        logger.warning(
            "Proxy lookup failed: model_id=%r not found or disabled (path=%s)",
            model_id,
            request_path,
        )
        raise HTTPException(
            status_code=404, detail=f"Model '{model_id}' not configured or disabled"
        )

    provider_type = model_config.provider.provider_type
    _validate_provider_path_compatibility(provider_type, request_path)
    audit_enabled = model_config.provider.audit_enabled
    audit_capture_bodies = model_config.provider.audit_capture_bodies
    provider_id = model_config.provider.id
    client: httpx.AsyncClient = request.app.state.http_client
    is_streaming = extract_stream_flag(raw_body) if raw_body else False
    client_headers = _get_client_headers(request)
    method = request.method
    upstream_model_id = model_config.model_id
    body_model_id = extract_model_from_body(raw_body) if raw_body else None
    rewritten_body = raw_body
    if raw_body and body_model_id and upstream_model_id != body_model_id:
        rewritten_body = _rewrite_model_in_body(raw_body, upstream_model_id)

    path_model = _extract_model_from_path(request_path)
    effective_request_path = request_path
    if path_model and upstream_model_id != path_model:
        effective_request_path = _rewrite_model_in_path(
            request_path, path_model, upstream_model_id
        )

    blocklist_rules: list[HeaderBlocklistRule] = list(
        (
            (
                await db.execute(
                    select(HeaderBlocklistRule).where(
                        HeaderBlocklistRule.enabled == True,  # noqa: E712
                        (HeaderBlocklistRule.is_system == True)  # noqa: E712
                        | (HeaderBlocklistRule.profile_id == profile_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    )

    now_mono = time.monotonic()
    endpoints_to_try = build_attempt_plan(profile_id, model_config, now_mono)
    if not endpoints_to_try:
        raise HTTPException(
            status_code=503,
            detail=f"No active connections available for model '{model_id}'. All connections may be in cooldown.",
        )

    costing_settings = await load_costing_settings(
        db,
        profile_id=profile_id,
        model_id=model_id,
        endpoint_ids=sorted(
            {
                endpoint.endpoint_id
                for endpoint in endpoints_to_try
                if endpoint.endpoint_id is not None
            }
        ),
    )

    def build_cost_fields(
        connection,
        status_code: int,
        tokens: dict[str, int | None] | None = None,
    ) -> CostFieldPayload:
        token_values = tokens or {}
        return compute_cost_fields(
            connection=connection,
            pricing_template=connection.pricing_template_rel,
            endpoint=connection.endpoint_rel,
            model_id=model_id,
            status_code=status_code,
            input_tokens=token_values.get("input_tokens"),
            output_tokens=token_values.get("output_tokens"),
            cache_read_input_tokens=token_values.get("cache_read_input_tokens"),
            cache_creation_input_tokens=token_values.get("cache_creation_input_tokens"),
            reasoning_tokens=token_values.get("reasoning_tokens"),
            settings=costing_settings,
        )

    recovery_active = (
        model_config.lb_strategy == "failover"
        and model_config.failover_recovery_enabled
    )
    last_error = None
    attempted_any_endpoint = False
    for ep in endpoints_to_try:
        if ep.endpoint_rel is None:
            logger.warning("Skipping connection %d because endpoint is missing", ep.id)
            continue
        if not await _endpoint_is_active_now(db, ep.id):
            mark_connection_recovered(profile_id, ep.id)
            logger.info(
                "Skipping endpoint %d because it is currently disabled",
                ep.id,
            )
            continue

        attempted_any_endpoint = True
        upstream_url = build_upstream_url(
            ep, effective_request_path, endpoint=ep.endpoint_rel
        )
        if request.url.query:
            upstream_url = f"{upstream_url}?{request.url.query}"
        headers = build_upstream_headers(
            ep,
            provider_type,
            client_headers,
            blocklist_rules,
            endpoint=ep.endpoint_rel,
        )
        ep_desc = ep.name
        endpoint_body = rewritten_body


        start_time = time.monotonic()

        try:
            if is_streaming:
                kwargs: dict = {"headers": headers}
                if endpoint_body:
                    kwargs["content"] = endpoint_body

                send_req = client.build_request(method, upstream_url, **kwargs)
                upstream_resp = await client.send(send_req, stream=True)

                resp_headers_filtered = filter_response_headers(upstream_resp.headers)
                elapsed_ms = int((time.monotonic() - start_time) * 1000)

                if upstream_resp.status_code >= 400:
                    body = await upstream_resp.aread()
                    await upstream_resp.aclose()

                    if should_failover(upstream_resp.status_code):
                        last_error = f"Upstream returned {upstream_resp.status_code}"
                        logger.warning(
                            f"Endpoint {ep.id} failed with {upstream_resp.status_code}, trying next"
                        )
                        rl_id = await log_request(
                            model_id=model_id,
                            profile_id=profile_id,
                            provider_type=provider_type,
                            endpoint_id=ep.endpoint_id,
                            connection_id=ep.id,
                            endpoint_base_url=ep.endpoint_rel.base_url,
                            endpoint_description=ep_desc,
                            status_code=upstream_resp.status_code,
                            response_time_ms=elapsed_ms,
                            is_stream=True,
                            request_path=request_path,
                            error_detail=body.decode("utf-8", errors="replace")[:500],
                            **build_cost_fields(ep, upstream_resp.status_code),
                        )
                        if audit_enabled:
                            await record_audit_log(
                                request_log_id=rl_id,
                                profile_id=profile_id,
                                provider_id=provider_id,
                                endpoint_id=ep.endpoint_id,
                                connection_id=ep.id,
                                endpoint_base_url=ep.endpoint_rel.base_url,
                                endpoint_description=ep_desc,
                                model_id=model_id,
                                request_method=method,
                                request_url=upstream_url,
                                request_headers=headers,
                                request_body=endpoint_body,
                                response_status=upstream_resp.status_code,
                                response_headers=dict(upstream_resp.headers),
                                response_body=body,
                                is_stream=True,
                                duration_ms=elapsed_ms,
                                capture_bodies=audit_capture_bodies,
                            )
                        if recovery_active:
                            mark_connection_failed(
                                profile_id,
                                ep.id,
                                model_config.failover_recovery_cooldown_seconds,
                                time.monotonic(),
                            )
                        continue

                    tokens = extract_token_usage(body)
                    rl_id = await log_request(
                        model_id=model_id,
                        profile_id=profile_id,
                        provider_type=provider_type,
                        endpoint_id=ep.endpoint_id,
                        connection_id=ep.id,
                        endpoint_base_url=ep.endpoint_rel.base_url,
                        endpoint_description=ep_desc,
                        status_code=upstream_resp.status_code,
                        response_time_ms=elapsed_ms,
                        is_stream=True,
                        request_path=request_path,
                        error_detail=body.decode("utf-8", errors="replace")[:500],
                        input_tokens=tokens.get("input_tokens"),
                        output_tokens=tokens.get("output_tokens"),
                        total_tokens=tokens.get("total_tokens"),
                        **build_cost_fields(ep, upstream_resp.status_code, tokens),
                    )
                    if audit_enabled:
                        await record_audit_log(
                            request_log_id=rl_id,
                            profile_id=profile_id,
                            provider_id=provider_id,
                            endpoint_id=ep.endpoint_id,
                            connection_id=ep.id,
                            endpoint_base_url=ep.endpoint_rel.base_url,
                            endpoint_description=ep_desc,
                            model_id=model_id,
                            request_method=method,
                            request_url=upstream_url,
                            request_headers=headers,
                            request_body=endpoint_body,
                            response_status=upstream_resp.status_code,
                            response_headers=dict(upstream_resp.headers),
                            response_body=body,
                            is_stream=True,
                            duration_ms=elapsed_ms,
                            capture_bodies=audit_capture_bodies,
                        )

                    if recovery_active:
                        mark_connection_recovered(profile_id, ep.id)
                    return Response(
                        content=body,
                        status_code=upstream_resp.status_code,
                        headers=resp_headers_filtered,
                    )

                # Success case - mark endpoint as recovered
                if recovery_active:
                    mark_connection_recovered(profile_id, ep.id)
                _log_model_id = model_id
                _log_provider_type = provider_type
                _log_endpoint_id = ep.id
                _log_endpoint_base_url = ep.endpoint_rel.base_url
                _log_endpoint_desc = ep_desc
                _log_endpoint = ep
                _log_status_code = upstream_resp.status_code
                _log_elapsed_ms = elapsed_ms
                _log_request_path = request_path
                _audit_enabled = audit_enabled
                _audit_capture_bodies = audit_capture_bodies
                _audit_provider_id = provider_id
                _audit_method = method
                _audit_upstream_url = upstream_url
                _audit_headers = headers
                _audit_raw_body = endpoint_body
                _audit_resp_headers = dict(upstream_resp.headers)

                async def _iter_and_log(
                    resp: httpx.Response,
                ) -> AsyncGenerator[bytes, None]:
                    accumulated = bytearray()
                    stream_cancelled = False
                    try:
                        async for chunk in resp.aiter_bytes():
                            if chunk:
                                accumulated.extend(chunk)
                                yield chunk
                    except GeneratorExit:
                        stream_cancelled = True
                        return
                    except asyncio.CancelledError:
                        stream_cancelled = True
                        logger.debug("Streaming response cancelled by client")
                        raise
                    except Exception as exc:
                        logger.warning("Stream iteration failed: %s", exc)
                    finally:
                        try:
                            await asyncio.shield(resp.aclose())
                        except BaseException:
                            pass

                        payload = bytes(accumulated)

                        async def _finalize_stream() -> None:
                            rl_id = None
                            try:
                                tokens = extract_token_usage(payload)
                                rl_id = await log_request(
                                    model_id=_log_model_id,
                                    profile_id=profile_id,
                                    provider_type=_log_provider_type,
                                    endpoint_id=_log_endpoint.endpoint_id,
                                    connection_id=_log_endpoint_id,
                                    endpoint_base_url=_log_endpoint_base_url,
                                    endpoint_description=_log_endpoint_desc,
                                    status_code=_log_status_code,
                                    response_time_ms=_log_elapsed_ms,
                                    is_stream=True,
                                    request_path=_log_request_path,
                                    input_tokens=tokens.get("input_tokens"),
                                    output_tokens=tokens.get("output_tokens"),
                                    total_tokens=tokens.get("total_tokens"),
                                    **build_cost_fields(
                                        _log_endpoint, _log_status_code, tokens
                                    ),
                                )
                            except asyncio.CancelledError:
                                logger.debug(
                                    "Streaming request logging cancelled before completion"
                                )
                                return
                            except Exception:
                                logger.exception("Failed to log streaming request")

                            if _audit_enabled:
                                try:
                                    await record_audit_log(
                                        request_log_id=rl_id,
                                        profile_id=profile_id,
                                        provider_id=_audit_provider_id,
                                        endpoint_id=_log_endpoint.endpoint_id,
                                        connection_id=_log_endpoint_id,
                                        endpoint_base_url=_log_endpoint_base_url,
                                        endpoint_description=_log_endpoint_desc,
                                        model_id=_log_model_id,
                                        request_method=_audit_method,
                                        request_url=_audit_upstream_url,
                                        request_headers=_audit_headers,
                                        request_body=_audit_raw_body,
                                        response_status=_log_status_code,
                                        response_headers=_audit_resp_headers,
                                        response_body=None,
                                        is_stream=True,
                                        duration_ms=_log_elapsed_ms,
                                        capture_bodies=_audit_capture_bodies,
                                    )
                                except asyncio.CancelledError:
                                    logger.debug(
                                        "Streaming audit logging cancelled before completion"
                                    )
                                except Exception:
                                    logger.exception(
                                        "Failed to record streaming audit log"
                                    )

                        try:
                            finalize_task = asyncio.create_task(
                                _finalize_stream(),
                                name="proxy-stream-finalize",
                            )
                        except RuntimeError:
                            logger.debug(
                                "Event loop closed before stream finalization could be scheduled"
                            )
                        else:
                            if stream_cancelled:
                                _track_detached_task(
                                    finalize_task,
                                    name="proxy stream finalization",
                                )
                            else:
                                try:
                                    await asyncio.shield(finalize_task)
                                except asyncio.CancelledError:
                                    _track_detached_task(
                                        finalize_task,
                                        name="proxy stream finalization",
                                    )
                                    raise

                media_type = upstream_resp.headers.get(
                    "content-type", "text/event-stream"
                )
                return StreamingResponse(
                    _iter_and_log(upstream_resp),
                    status_code=upstream_resp.status_code,
                    media_type=media_type,
                    headers={
                        **resp_headers_filtered,
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                    },
                )
            else:
                response = await proxy_request(
                    client,
                    method,
                    upstream_url,
                    headers,
                    endpoint_body,
                )
                elapsed_ms = int((time.monotonic() - start_time) * 1000)
                resp_headers = filter_response_headers(response.headers)

                if response.status_code >= 400 and should_failover(
                    response.status_code
                ):
                    last_error = f"Upstream returned {response.status_code}"
                    logger.warning(
                        f"Endpoint {ep.id} failed with {response.status_code}, trying next"
                    )
                    rl_id = await log_request(
                        model_id=model_id,
                        profile_id=profile_id,
                        provider_type=provider_type,
                        endpoint_id=ep.endpoint_id,
                        connection_id=ep.id,
                        endpoint_base_url=ep.endpoint_rel.base_url,
                        endpoint_description=ep_desc,
                        status_code=response.status_code,
                        response_time_ms=elapsed_ms,
                        is_stream=False,
                        request_path=request_path,
                        error_detail=response.content.decode("utf-8", errors="replace")[
                            :500
                        ],
                        **build_cost_fields(ep, response.status_code),
                    )
                    if audit_enabled:
                        await record_audit_log(
                            request_log_id=rl_id,
                            profile_id=profile_id,
                            provider_id=provider_id,
                            endpoint_id=ep.endpoint_id,
                            connection_id=ep.id,
                            endpoint_base_url=ep.endpoint_rel.base_url,
                            endpoint_description=ep_desc,
                            model_id=model_id,
                            request_method=method,
                            request_url=upstream_url,
                            request_headers=headers,
                            request_body=endpoint_body,
                            response_status=response.status_code,
                            response_headers=dict(response.headers),
                            response_body=response.content,
                            is_stream=False,
                            duration_ms=elapsed_ms,
                            capture_bodies=audit_capture_bodies,
                        )
                    if recovery_active:
                        mark_connection_failed(
                            profile_id,
                            ep.id,
                            model_config.failover_recovery_cooldown_seconds,
                            time.monotonic(),
                        )
                    continue

                tokens = extract_token_usage(response.content)
                error_detail = None
                if response.status_code >= 400:
                    error_detail = response.content.decode("utf-8", errors="replace")[
                        :500
                    ]

                rl_id = await log_request(
                    model_id=model_id,
                    profile_id=profile_id,
                    provider_type=provider_type,
                    endpoint_id=ep.endpoint_id,
                    connection_id=ep.id,
                    endpoint_base_url=ep.endpoint_rel.base_url,
                    endpoint_description=ep_desc,
                    status_code=response.status_code,
                    response_time_ms=elapsed_ms,
                    is_stream=False,
                    request_path=request_path,
                    error_detail=error_detail,
                    input_tokens=tokens.get("input_tokens"),
                    output_tokens=tokens.get("output_tokens"),
                    total_tokens=tokens.get("total_tokens"),
                    **build_cost_fields(ep, response.status_code, tokens),
                )
                if audit_enabled:
                    await record_audit_log(
                        request_log_id=rl_id,
                        profile_id=profile_id,
                        provider_id=provider_id,
                        endpoint_id=ep.endpoint_id,
                        connection_id=ep.id,
                        endpoint_base_url=ep.endpoint_rel.base_url,
                        endpoint_description=ep_desc,
                        model_id=model_id,
                        request_method=method,
                        request_url=upstream_url,
                        request_headers=headers,
                        request_body=endpoint_body,
                        response_status=response.status_code,
                        response_headers=dict(response.headers),
                        response_body=response.content,
                        is_stream=False,
                        duration_ms=elapsed_ms,
                        capture_bodies=audit_capture_bodies,
                    )

                # Success or non-failover error - mark as recovered if in failover mode
                if recovery_active:
                    mark_connection_recovered(profile_id, ep.id)
                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    headers=resp_headers,
                )

        except httpx.ConnectError as e:
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            last_error = f"Connection error: {e}"
            logger.warning(f"Endpoint {ep.id} connection failed: {e}")
            rl_id = await log_request(
                model_id=model_id,
                profile_id=profile_id,
                provider_type=provider_type,
                endpoint_id=ep.endpoint_id,
                connection_id=ep.id,
                endpoint_base_url=ep.endpoint_rel.base_url,
                endpoint_description=ep_desc,
                status_code=0,
                response_time_ms=elapsed_ms,
                is_stream=is_streaming,
                request_path=request_path,
                error_detail=str(e)[:500],
                **build_cost_fields(ep, 0),
            )
            if audit_enabled:
                await record_audit_log(
                    request_log_id=rl_id,
                    profile_id=profile_id,
                    provider_id=provider_id,
                    endpoint_id=ep.endpoint_id,
                    connection_id=ep.id,
                    endpoint_base_url=ep.endpoint_rel.base_url,
                    endpoint_description=ep_desc,
                    model_id=model_id,
                    request_method=method,
                    request_url=upstream_url,
                    request_headers=headers,
                    request_body=endpoint_body,
                    response_status=0,
                    response_headers=None,
                    response_body=None,
                    is_stream=is_streaming,
                    duration_ms=elapsed_ms,
                    capture_bodies=audit_capture_bodies,
                )
            if recovery_active:
                mark_connection_failed(
                    profile_id,
                    ep.id,
                    model_config.failover_recovery_cooldown_seconds,
                    time.monotonic(),
                )
            continue
        except httpx.TimeoutException as e:
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            last_error = f"Timeout: {e}"
            logger.warning(f"Endpoint {ep.id} timed out: {e}")
            rl_id = await log_request(
                model_id=model_id,
                profile_id=profile_id,
                provider_type=provider_type,
                endpoint_id=ep.endpoint_id,
                connection_id=ep.id,
                endpoint_base_url=ep.endpoint_rel.base_url,
                endpoint_description=ep_desc,
                status_code=0,
                response_time_ms=elapsed_ms,
                is_stream=is_streaming,
                request_path=request_path,
                error_detail=str(e)[:500],
                **build_cost_fields(ep, 0),
            )
            if audit_enabled:
                await record_audit_log(
                    request_log_id=rl_id,
                    profile_id=profile_id,
                    provider_id=provider_id,
                    endpoint_id=ep.endpoint_id,
                    connection_id=ep.id,
                    endpoint_base_url=ep.endpoint_rel.base_url,
                    endpoint_description=ep_desc,
                    model_id=model_id,
                    request_method=method,
                    request_url=upstream_url,
                    request_headers=headers,
                    request_body=endpoint_body,
                    response_status=0,
                    response_headers=None,
                    response_body=None,
                    is_stream=is_streaming,
                    duration_ms=elapsed_ms,
                    capture_bodies=audit_capture_bodies,
                )
            if recovery_active:
                mark_connection_failed(
                    profile_id,
                    ep.id,
                    model_config.failover_recovery_cooldown_seconds,
                    time.monotonic(),
                )
            continue

    if not attempted_any_endpoint:
        raise HTTPException(
            status_code=503,
            detail=f"No active connections available for model '{model_id}'.",
        )

    raise HTTPException(
        status_code=502,
        detail=f"All connections failed for model '{model_id}'. Last error: {last_error}",
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
