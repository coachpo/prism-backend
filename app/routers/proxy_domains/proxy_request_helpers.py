import asyncio
import json
import logging
import re
from typing import cast

import httpx
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Connection
from app.services.loadbalancer.types import FailureKind
from app.services.proxy_service import extract_model_from_body

logger = logging.getLogger(__name__)

_GEMINI_MODEL_RE = re.compile(r"^/v1beta/models/([^/:]+)")
_GEMINI_NATIVE_PATH_RE = re.compile(r"^/v1beta/models/[^/:]+(?:[:/].*)?/?$")
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


def track_detached_task(task: asyncio.Task[None], *, name: str) -> None:
    _track_detached_task(task, name=name)


def _get_client_headers(request: Request) -> dict[str, str]:
    return dict(request.headers)


def get_client_headers(request: Request) -> dict[str, str]:
    return _get_client_headers(request)


def _extract_model_from_path(request_path: str) -> str | None:
    match = _GEMINI_MODEL_RE.search(request_path)
    return match.group(1) if match else None


def extract_model_from_path(request_path: str) -> str | None:
    return _extract_model_from_path(request_path)


def _rewrite_model_in_path(
    request_path: str, original_model: str, target_model: str
) -> str:
    if original_model == target_model:
        return request_path
    return request_path.replace(
        f"/models/{original_model}", f"/models/{target_model}", 1
    )


def rewrite_model_in_path(
    request_path: str, original_model: str, target_model: str
) -> str:
    return _rewrite_model_in_path(request_path, original_model, target_model)


async def _endpoint_is_active_now(
    db: AsyncSession, connection_id: int, profile_id: int | None = None
) -> bool:
    query = select(Connection.is_active).where(Connection.id == connection_id)
    if profile_id is not None:
        query = query.where(Connection.profile_id == profile_id)
    result = await db.execute(query)
    return bool(result.scalar_one_or_none())


async def endpoint_is_active_now(
    db: AsyncSession, connection_id: int, profile_id: int | None = None
) -> bool:
    return await _endpoint_is_active_now(db, connection_id, profile_id)


def _resolve_model_id(raw_body: bytes | None, request_path: str) -> str | None:
    if not raw_body:
        return _extract_model_from_path(request_path)
    model_id = extract_model_from_body(raw_body)
    if model_id:
        return model_id
    # Gemini-style requests can carry model in path instead of JSON body.
    return _extract_model_from_path(request_path)


def resolve_model_id(raw_body: bytes | None, request_path: str) -> str | None:
    return _resolve_model_id(raw_body, request_path)


def _classify_request_path(request_path: str) -> str:
    if _GEMINI_NATIVE_PATH_RE.match(request_path):
        return "gemini_native"
    if _ANTHROPIC_MESSAGES_PATH_RE.match(request_path):
        return "anthropic_messages"
    return "generic"


_PROVIDER_PATH_FAMILIES: dict[str, set[str]] = {
    "openai": {"generic"},
    "anthropic": {"anthropic_messages"},
    "gemini": {"gemini_native"},
}


def _validate_provider_path_compatibility(
    provider_type: str, request_path: str
) -> None:
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


def validate_provider_path_compatibility(provider_type: str, request_path: str) -> None:
    _validate_provider_path_compatibility(provider_type, request_path)


def _rewrite_model_in_body(raw_body: bytes, target_model_id: str) -> bytes:
    try:
        payload = cast(object, json.loads(raw_body))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return raw_body
    payload_dict = _as_object_dict(payload)
    if payload_dict is None:
        return raw_body
    payload_dict["model"] = target_model_id
    try:
        return json.dumps(payload_dict).encode("utf-8")
    except (TypeError, ValueError):
        return raw_body


def rewrite_model_in_body(raw_body: bytes, target_model_id: str) -> bytes:
    return _rewrite_model_in_body(raw_body, target_model_id)


def _inject_openai_stream_usage_option(
    raw_body: bytes,
    provider_type: str,
    request_path: str,
) -> bytes:
    if provider_type != "openai":
        return raw_body

    if not request_path.rstrip("/").endswith("/chat/completions"):
        return raw_body

    try:
        payload = cast(object, json.loads(raw_body))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return raw_body

    payload_dict = _as_object_dict(payload)
    if payload_dict is None:
        return raw_body

    stream_options_value = payload_dict.get("stream_options")
    stream_options_dict = _as_object_dict(stream_options_value)
    stream_options = (
        dict(stream_options_dict) if stream_options_dict is not None else {}
    )
    stream_options["include_usage"] = True
    payload_dict["stream_options"] = stream_options

    try:
        return json.dumps(payload_dict, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return raw_body


def inject_openai_stream_usage_option(
    raw_body: bytes,
    provider_type: str,
    request_path: str,
) -> bytes:
    return _inject_openai_stream_usage_option(raw_body, provider_type, request_path)


_AUTH_LIKE_ERROR_RE = re.compile(
    r"(auth|authoriz|forbidden|permission|api[\s_-]?key|token|credential|access denied)",
    re.IGNORECASE,
)


def _as_object_dict(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return None


def _extract_error_text(raw_body: bytes | None) -> str:
    if not raw_body:
        return ""
    decoded_body = raw_body.decode("utf-8", errors="replace")
    text_chunks = [decoded_body]
    try:
        payload = cast(object, json.loads(raw_body))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return decoded_body

    payload_dict = _as_object_dict(payload)
    if payload_dict is not None:
        error_value = payload_dict.get("error")
        error_dict = _as_object_dict(error_value)
        if error_dict is not None:
            for field in ("message", "detail", "type", "code"):
                value = error_dict.get(field)
                if isinstance(value, str):
                    text_chunks.append(value)
        elif isinstance(error_value, str):
            text_chunks.append(error_value)

        for field in ("detail", "message"):
            value = payload_dict.get(field)
            if isinstance(value, str):
                text_chunks.append(value)
    elif isinstance(payload, str):
        text_chunks.append(payload)

    return " ".join(chunk for chunk in text_chunks if chunk)


def extract_error_text(raw_body: bytes | None) -> str:
    return _extract_error_text(raw_body)


def _classify_http_failure(status_code: int, raw_body: bytes | None) -> FailureKind:
    if status_code != 403:
        return "transient_http"
    return (
        "auth_like"
        if _AUTH_LIKE_ERROR_RE.search(_extract_error_text(raw_body))
        else "transient_http"
    )


def classify_http_failure(status_code: int, raw_body: bytes | None) -> FailureKind:
    return _classify_http_failure(status_code, raw_body)


def _classify_failover_failure(
    *,
    status_code: int | None = None,
    raw_body: bytes | None = None,
    exception: Exception | None = None,
) -> FailureKind:
    if exception is not None:
        return (
            "timeout"
            if isinstance(exception, httpx.TimeoutException)
            else "connect_error"
        )
    if status_code is None:
        return "transient_http"
    return _classify_http_failure(status_code, raw_body)


def classify_failover_failure(
    *,
    status_code: int | None = None,
    raw_body: bytes | None = None,
    exception: Exception | None = None,
) -> FailureKind:
    return _classify_failover_failure(
        status_code=status_code,
        raw_body=raw_body,
        exception=exception,
    )


def _is_recovery_success_status(status_code: int) -> bool:
    return 200 <= status_code < 400


def is_recovery_success_status(status_code: int) -> bool:
    return _is_recovery_success_status(status_code)
