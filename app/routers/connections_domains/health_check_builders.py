import logging

import httpx

from app.models.models import Connection, Endpoint
from app.services.proxy_service import build_upstream_url

from .health_check_request_helpers import _execute_health_check_request

logger = logging.getLogger(__name__)


def _build_health_check_request(
    api_family: str, model_id: str
) -> tuple[str, dict[str, object]]:
    if api_family == "openai":
        return "/v1/responses", {
            "model": model_id,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hi"}],
                }
            ],
            "max_output_tokens": 1,
        }
    if api_family == "anthropic":
        return "/v1/messages", {
            "model": model_id,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }
    if api_family == "gemini":
        return f"/v1beta/models/{model_id}:generateContent", {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": "hi"}],
                }
            ],
            "generationConfig": {"maxOutputTokens": 1},
        }
    raise ValueError(f"Unsupported api_family '{api_family}' for health check")


def _build_openai_legacy_health_check_request(
    model_id: str,
) -> tuple[str, dict[str, object]]:
    return "/v1/chat/completions", {
        "model": model_id,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }


def _build_openai_responses_basic_health_check_request(
    model_id: str,
) -> tuple[str, dict[str, object]]:
    return "/v1/responses", {
        "model": model_id,
        "input": "hi",
    }


async def _probe_connection_health(
    *,
    client: httpx.AsyncClient,
    connection: Connection,
    endpoint: Endpoint,
    api_family: str,
    model_id: str,
    headers: dict[str, str],
    execute_health_check_request_fn=_execute_health_check_request,
) -> tuple[str, str, int, str]:
    request_path, body = _build_health_check_request(api_family, model_id)
    upstream_url = build_upstream_url(connection, request_path, endpoint=endpoint)
    health_status, detail, response_time_ms = await execute_health_check_request_fn(
        client,
        upstream_url=upstream_url,
        headers=headers,
        body=body,
    )
    log_url = upstream_url

    if api_family == "openai" and health_status != "healthy":
        responses_basic_path, responses_basic_body = (
            _build_openai_responses_basic_health_check_request(model_id)
        )
        responses_basic_url = build_upstream_url(
            connection, responses_basic_path, endpoint=endpoint
        )
        (
            responses_basic_status,
            responses_basic_detail,
            responses_basic_response_time_ms,
        ) = await execute_health_check_request_fn(
            client,
            upstream_url=responses_basic_url,
            headers=headers,
            body=responses_basic_body,
        )
        if responses_basic_status == "healthy":
            return (
                "healthy",
                f"{responses_basic_detail} (fallback /v1/responses basic input)",
                responses_basic_response_time_ms,
                responses_basic_url,
            )

        fallback_path, fallback_body = _build_openai_legacy_health_check_request(
            model_id
        )
        fallback_url = build_upstream_url(connection, fallback_path, endpoint=endpoint)
        (
            fallback_status,
            fallback_detail,
            fallback_response_time_ms,
        ) = await execute_health_check_request_fn(
            client,
            upstream_url=fallback_url,
            headers=headers,
            body=fallback_body,
        )
        if fallback_status == "healthy":
            return (
                "healthy",
                f"{fallback_detail} (legacy fallback /v1/chat/completions)",
                fallback_response_time_ms,
                fallback_url,
            )
        detail_parts = [
            detail,
            f"fallback /v1/responses basic input failed: {responses_basic_detail}",
            f"fallback /v1/chat/completions failed: {fallback_detail}",
        ]
        detail = "; ".join(part for part in detail_parts if part)
        response_time_ms = (
            fallback_response_time_ms
            or responses_basic_response_time_ms
            or response_time_ms
        )
        log_url = f"{upstream_url} -> {responses_basic_url} -> {fallback_url}"

    return health_status, detail, response_time_ms, log_url


__all__ = [
    "_build_health_check_request",
    "_build_openai_legacy_health_check_request",
    "_build_openai_responses_basic_health_check_request",
    "_probe_connection_health",
]
