import logging

import httpx

from app.models.models import Connection, Endpoint
from app.services.monitoring.probe_runner import (
    _execute_monitoring_probe_checks,
    _build_monitoring_conversation_request,
    _build_monitoring_endpoint_ping_request,
)
from app.services.proxy_service import build_upstream_url

from .health_check_request_helpers import _execute_health_check_request

logger = logging.getLogger(__name__)


def _build_health_check_request(
    api_family: str,
    model_id: str,
    *,
    openai_variant: str = "responses_minimal",
) -> tuple[str, dict[str, object]]:
    return _build_monitoring_conversation_request(
        api_family,
        model_id,
        openai_variant=openai_variant,
    )


def _build_openai_chat_completions_health_check_request(
    model_id: str,
) -> tuple[str, dict[str, object]]:
    return _build_monitoring_conversation_request(
        "openai",
        model_id,
        openai_variant="chat_completions_minimal",
    )


def _build_openai_responses_basic_health_check_request(
    model_id: str,
) -> tuple[str, dict[str, object]]:
    return _build_monitoring_conversation_request(
        "openai",
        model_id,
        openai_variant="responses_minimal",
    )


def _build_endpoint_ping_request(
    api_family: str,
    model_id: str,
    *,
    openai_variant: str = "responses_minimal",
) -> tuple[str, dict[str, object]]:
    return _build_monitoring_endpoint_ping_request(
        api_family,
        model_id,
        openai_variant=openai_variant,
    )


async def _probe_connection_health(
    *,
    client: httpx.AsyncClient,
    connection: Connection,
    endpoint: Endpoint,
    api_family: str,
    model_id: str,
    headers: dict[str, str],
    openai_variant: str = "responses_minimal",
    execute_health_check_request_fn=_execute_health_check_request,
) -> tuple[str, str, int, str]:
    result = await _execute_monitoring_probe_checks(
        client=client,
        connection=connection,
        endpoint=endpoint,
        api_family=api_family,
        model_id=model_id,
        headers=headers,
        openai_variant=openai_variant,
        execute_probe_request_fn=execute_health_check_request_fn,
    )
    return (
        result.health_status,
        result.detail,
        result.conversation_delay_ms or result.endpoint_ping_ms or 0,
        result.log_url,
    )


__all__ = [
    "_build_health_check_request",
    "_build_endpoint_ping_request",
    "_build_openai_chat_completions_health_check_request",
    "_build_openai_responses_basic_health_check_request",
    "_probe_connection_health",
]
