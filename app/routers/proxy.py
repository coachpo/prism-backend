import logging
import re
import time
from typing import Annotated, AsyncGenerator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.services.loadbalancer import (
    get_model_config_with_endpoints,
    select_endpoint,
    get_failover_candidates,
)
from app.services.proxy_service import (
    build_upstream_url,
    build_upstream_headers,
    proxy_request,
    should_failover,
    extract_model_from_body,
    extract_stream_flag,
    filter_response_headers,
    rewrite_model_in_body,
    inject_stream_options,
)
from app.services.stats_service import log_request, extract_token_usage
from app.services.audit_service import record_audit_log

logger = logging.getLogger(__name__)

router = APIRouter(tags=["proxy"])

MODEL_ID_HEADER = "x-model-id"

# Gemini URL pattern: /v1beta/models/{model_id}:{action}
_GEMINI_MODEL_RE = re.compile(r"/models/([^/:]+)")


def _get_client_headers(request: Request) -> dict[str, str]:
    return dict(request.headers)


def _extract_model_from_path(request_path: str) -> str | None:
    m = _GEMINI_MODEL_RE.search(request_path)
    return m.group(1) if m else None


def _rewrite_model_in_path(
    request_path: str, original_model: str, target_model: str
) -> str:
    if original_model == target_model:
        return request_path
    return request_path.replace(
        f"/models/{original_model}", f"/models/{target_model}", 1
    )


def _resolve_model_id(
    request: Request, raw_body: bytes | None, request_path: str
) -> str | None:
    if raw_body:
        model_id = extract_model_from_body(raw_body)
        if model_id:
            return model_id
    header_id = request.headers.get(MODEL_ID_HEADER)
    if header_id:
        return header_id
    # Gemini-style: model is in the URL path, not the body
    return _extract_model_from_path(request_path)


async def _handle_proxy(
    request: Request,
    db: AsyncSession,
    raw_body: bytes | None,
    request_path: str,
):
    model_id = _resolve_model_id(request, raw_body, request_path)
    if not model_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot determine model for routing. "
                "Include 'model' in the request body or set the X-Model-Id header."
            ),
        )

    model_config = await get_model_config_with_endpoints(db, model_id)
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
    audit_enabled = model_config.provider.audit_enabled
    audit_capture_bodies = model_config.provider.audit_capture_bodies
    provider_id = model_config.provider.id
    client: httpx.AsyncClient = request.app.state.http_client
    is_streaming = extract_stream_flag(raw_body) if raw_body else False
    client_headers = _get_client_headers(request)
    method = request.method

    upstream_model_id = model_config.model_id
    if raw_body and upstream_model_id != model_id:
        raw_body = rewrite_model_in_body(raw_body, upstream_model_id)
    elif raw_body and not extract_model_from_body(raw_body):
        raw_body = rewrite_model_in_body(raw_body, upstream_model_id)

    path_model = _extract_model_from_path(request_path)
    effective_request_path = request_path
    if path_model and upstream_model_id != model_id:
        effective_request_path = _rewrite_model_in_path(
            request_path, path_model, upstream_model_id
        )

    if is_streaming and raw_body:
        raw_body = inject_stream_options(raw_body, provider_type)

    endpoint = select_endpoint(model_config)
    if not endpoint:
        raise HTTPException(
            status_code=503, detail=f"No active endpoints for model '{model_id}'"
        )

    endpoints_to_try = [endpoint] + get_failover_candidates(model_config, endpoint.id)

    last_error = None
    for ep in endpoints_to_try:
        upstream_url = build_upstream_url(ep, effective_request_path)
        if request.url.query:
            upstream_url = f"{upstream_url}?{request.url.query}"
        headers = build_upstream_headers(ep, provider_type, client_headers)
        ep_desc = ep.description

        start_time = time.monotonic()

        try:
            if is_streaming:
                kwargs: dict = {"headers": headers}
                if raw_body:
                    kwargs["content"] = raw_body

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
                            provider_type=provider_type,
                            endpoint_id=ep.id,
                            endpoint_base_url=ep.base_url,
                            endpoint_description=ep_desc,
                            status_code=upstream_resp.status_code,
                            response_time_ms=elapsed_ms,
                            is_stream=True,
                            request_path=request_path,
                            error_detail=body.decode("utf-8", errors="replace")[:500],
                        )
                        if audit_enabled:
                            await record_audit_log(
                                request_log_id=rl_id,
                                provider_id=provider_id,
                                endpoint_id=ep.id,
                                endpoint_base_url=ep.base_url,
                                endpoint_description=ep_desc,
                                model_id=model_id,
                                request_method=method,
                                request_url=upstream_url,
                                request_headers=headers,
                                request_body=raw_body,
                                response_status=upstream_resp.status_code,
                                response_headers=dict(upstream_resp.headers),
                                response_body=body,
                                is_stream=True,
                                duration_ms=elapsed_ms,
                                capture_bodies=audit_capture_bodies,
                            )
                        continue

                    tokens = extract_token_usage(body)
                    rl_id = await log_request(
                        model_id=model_id,
                        provider_type=provider_type,
                        endpoint_id=ep.id,
                        endpoint_base_url=ep.base_url,
                        endpoint_description=ep_desc,
                        status_code=upstream_resp.status_code,
                        response_time_ms=elapsed_ms,
                        is_stream=True,
                        request_path=request_path,
                        error_detail=body.decode("utf-8", errors="replace")[:500],
                        **tokens,
                    )
                    if audit_enabled:
                        await record_audit_log(
                            request_log_id=rl_id,
                            provider_id=provider_id,
                            endpoint_id=ep.id,
                            endpoint_base_url=ep.base_url,
                            endpoint_description=ep_desc,
                            model_id=model_id,
                            request_method=method,
                            request_url=upstream_url,
                            request_headers=headers,
                            request_body=raw_body,
                            response_status=upstream_resp.status_code,
                            response_headers=dict(upstream_resp.headers),
                            response_body=body,
                            is_stream=True,
                            duration_ms=elapsed_ms,
                            capture_bodies=audit_capture_bodies,
                        )

                    return Response(
                        content=body,
                        status_code=upstream_resp.status_code,
                        headers=resp_headers_filtered,
                    )

                _log_model_id = model_id
                _log_provider_type = provider_type
                _log_endpoint_id = ep.id
                _log_endpoint_base_url = ep.base_url
                _log_endpoint_desc = ep_desc
                _log_status_code = upstream_resp.status_code
                _log_elapsed_ms = elapsed_ms
                _log_request_path = request_path
                _audit_enabled = audit_enabled
                _audit_capture_bodies = audit_capture_bodies
                _audit_provider_id = provider_id
                _audit_method = method
                _audit_upstream_url = upstream_url
                _audit_headers = headers
                _audit_raw_body = raw_body
                _audit_resp_headers = dict(upstream_resp.headers)

                async def _iter_and_log(
                    resp: httpx.Response,
                ) -> AsyncGenerator[bytes, None]:
                    rl_id = None
                    accumulated = bytearray()
                    try:
                        async for chunk in resp.aiter_bytes():
                            if chunk:
                                accumulated.extend(chunk)
                                yield chunk
                    except GeneratorExit:
                        return
                    except BaseException as e:
                        logger.error(f"Stream error: {e}")
                    finally:
                        try:
                            await resp.aclose()
                        except BaseException:
                            pass
                        try:
                            tokens = extract_token_usage(bytes(accumulated))
                            rl_id = await log_request(
                                model_id=_log_model_id,
                                provider_type=_log_provider_type,
                                endpoint_id=_log_endpoint_id,
                                endpoint_base_url=_log_endpoint_base_url,
                                endpoint_description=_log_endpoint_desc,
                                status_code=_log_status_code,
                                response_time_ms=_log_elapsed_ms,
                                is_stream=True,
                                request_path=_log_request_path,
                                **tokens,
                            )
                        except BaseException:
                            logger.exception("Failed to log streaming request")
                        if _audit_enabled:
                            try:
                                await record_audit_log(
                                    request_log_id=rl_id,
                                    provider_id=_audit_provider_id,
                                    endpoint_id=_log_endpoint_id,
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
                            except BaseException:
                                logger.exception("Failed to record streaming audit log")

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
                    raw_body,
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
                        provider_type=provider_type,
                        endpoint_id=ep.id,
                        endpoint_base_url=ep.base_url,
                        endpoint_description=ep_desc,
                        status_code=response.status_code,
                        response_time_ms=elapsed_ms,
                        is_stream=False,
                        request_path=request_path,
                        error_detail=response.content.decode("utf-8", errors="replace")[
                            :500
                        ],
                    )
                    if audit_enabled:
                        await record_audit_log(
                            request_log_id=rl_id,
                            provider_id=provider_id,
                            endpoint_id=ep.id,
                            endpoint_base_url=ep.base_url,
                            endpoint_description=ep_desc,
                            model_id=model_id,
                            request_method=method,
                            request_url=upstream_url,
                            request_headers=headers,
                            request_body=raw_body,
                            response_status=response.status_code,
                            response_headers=dict(response.headers),
                            response_body=response.content,
                            is_stream=False,
                            duration_ms=elapsed_ms,
                            capture_bodies=audit_capture_bodies,
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
                    provider_type=provider_type,
                    endpoint_id=ep.id,
                    endpoint_base_url=ep.base_url,
                    endpoint_description=ep_desc,
                    status_code=response.status_code,
                    response_time_ms=elapsed_ms,
                    is_stream=False,
                    request_path=request_path,
                    error_detail=error_detail,
                    **tokens,
                )
                if audit_enabled:
                    await record_audit_log(
                        request_log_id=rl_id,
                        provider_id=provider_id,
                        endpoint_id=ep.id,
                        endpoint_base_url=ep.base_url,
                        endpoint_description=ep_desc,
                        model_id=model_id,
                        request_method=method,
                        request_url=upstream_url,
                        request_headers=headers,
                        request_body=raw_body,
                        response_status=response.status_code,
                        response_headers=dict(response.headers),
                        response_body=response.content,
                        is_stream=False,
                        duration_ms=elapsed_ms,
                        capture_bodies=audit_capture_bodies,
                    )

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
                provider_type=provider_type,
                endpoint_id=ep.id,
                endpoint_base_url=ep.base_url,
                endpoint_description=ep_desc,
                status_code=0,
                response_time_ms=elapsed_ms,
                is_stream=is_streaming,
                request_path=request_path,
                error_detail=str(e)[:500],
            )
            if audit_enabled:
                await record_audit_log(
                    request_log_id=rl_id,
                    provider_id=provider_id,
                    endpoint_id=ep.id,
                    endpoint_base_url=ep.base_url,
                    endpoint_description=ep_desc,
                    model_id=model_id,
                    request_method=method,
                    request_url=upstream_url,
                    request_headers=headers,
                    request_body=raw_body,
                    response_status=0,
                    response_headers=None,
                    response_body=None,
                    is_stream=is_streaming,
                    duration_ms=elapsed_ms,
                    capture_bodies=audit_capture_bodies,
                )
            continue
        except httpx.TimeoutException as e:
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            last_error = f"Timeout: {e}"
            logger.warning(f"Endpoint {ep.id} timed out: {e}")
            rl_id = await log_request(
                model_id=model_id,
                provider_type=provider_type,
                endpoint_id=ep.id,
                endpoint_base_url=ep.base_url,
                endpoint_description=ep_desc,
                status_code=0,
                response_time_ms=elapsed_ms,
                is_stream=is_streaming,
                request_path=request_path,
                error_detail=str(e)[:500],
            )
            if audit_enabled:
                await record_audit_log(
                    request_log_id=rl_id,
                    provider_id=provider_id,
                    endpoint_id=ep.id,
                    endpoint_base_url=ep.base_url,
                    endpoint_description=ep_desc,
                    model_id=model_id,
                    request_method=method,
                    request_url=upstream_url,
                    request_headers=headers,
                    request_body=raw_body,
                    response_status=0,
                    response_headers=None,
                    response_body=None,
                    is_stream=is_streaming,
                    duration_ms=elapsed_ms,
                    capture_bodies=audit_capture_bodies,
                )
            continue
        except httpx.HTTPStatusError as e:
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            last_error = f"HTTP error: {e}"
            logger.warning(f"Endpoint {ep.id} HTTP error: {e}")
            resp_status = e.response.status_code if e.response else 0
            rl_id = await log_request(
                model_id=model_id,
                provider_type=provider_type,
                endpoint_id=ep.id,
                endpoint_base_url=ep.base_url,
                endpoint_description=ep_desc,
                status_code=resp_status,
                response_time_ms=elapsed_ms,
                is_stream=is_streaming,
                request_path=request_path,
                error_detail=str(e)[:500],
            )
            if audit_enabled:
                await record_audit_log(
                    request_log_id=rl_id,
                    provider_id=provider_id,
                    endpoint_id=ep.id,
                    endpoint_base_url=ep.base_url,
                    endpoint_description=ep_desc,
                    model_id=model_id,
                    request_method=method,
                    request_url=upstream_url,
                    request_headers=headers,
                    request_body=raw_body,
                    response_status=resp_status,
                    response_headers=dict(e.response.headers) if e.response else None,
                    response_body=e.response.content if e.response else None,
                    is_stream=is_streaming,
                    duration_ms=elapsed_ms,
                    capture_bodies=audit_capture_bodies,
                )
            continue

    raise HTTPException(
        status_code=502,
        detail=f"All endpoints failed for model '{model_id}'. Last error: {last_error}",
    )


@router.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_catch_all(
    request: Request,
    path: str,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
):
    raw_body = await request.body() or None
    return await _handle_proxy(request, db, raw_body, f"/v1/{path}")


@router.api_route(
    "/v1beta/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
)
async def proxy_catch_all_v1beta(
    request: Request,
    path: str,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
):
    raw_body = await request.body() or None
    return await _handle_proxy(request, db, raw_body, f"/v1beta/{path}")
