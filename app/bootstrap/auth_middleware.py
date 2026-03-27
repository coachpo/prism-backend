from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from app.core import database as database_core
from app.core.auth import decode_access_token, extract_proxy_api_key
from app.core.config import get_settings
from app.core.time import utc_now
from app.services.auth_service import (
    enqueue_proxy_api_key_usage,
    get_app_auth_settings_snapshot,
    persist_proxy_api_key_usage,
    verify_proxy_api_key,
)

PUBLIC_MANAGEMENT_PATHS = {
    "/api/auth/status",
    "/api/auth/public-bootstrap",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/refresh",
    "/api/auth/password-reset/request",
    "/api/auth/password-reset/confirm",
    "/api/auth/webauthn/authenticate/options",
    "/api/auth/webauthn/authenticate/verify",
}

CallNext = Callable[[Request], Awaitable[Response]]


def build_auth_error_response(
    request: Request, *, status_code: int, detail: str
) -> JSONResponse:
    response = JSONResponse(status_code=status_code, content={"detail": detail})
    origin = request.headers.get("origin")
    allowed_origins = get_settings().cors_allowed_origins_list
    if origin and origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"
    return response


def _requires_auth_handling(path: str) -> bool:
    return (
        path.startswith("/api/")
        or path.startswith("/v1/")
        or path.startswith("/v1beta/")
    )


def _set_request_auth_state(request: Request, *, auth_enabled: bool) -> None:
    request.state.auth_enabled = auth_enabled
    request.state.auth_subject = None
    request.state.proxy_api_key_id = None
    request.state.proxy_api_key_name = None


def _append_response_background_task(
    response: Response,
    task: Callable[[], Awaitable[None]],
) -> None:
    existing_background = response.background

    async def run_background_chain() -> None:
        if existing_background is not None:
            await existing_background()
        await task()

    response.background = BackgroundTask(run_background_chain)


def _get_authenticated_subject(auth_settings, token_payload: dict[str, object]):
    payload_subject = token_payload.get("sub")
    payload_token_version = token_payload.get("token_version")
    try:
        subject_id = int(str(payload_subject))
        token_version = int(str(payload_token_version))
    except (TypeError, ValueError):
        return None

    if subject_id != auth_settings.id or token_version != auth_settings.token_version:
        return None

    return {
        "id": auth_settings.id,
        "username": auth_settings.username,
        "token_version": auth_settings.token_version,
    }


async def _handle_management_authentication(
    request: Request,
    call_next: CallNext,
    *,
    auth_settings,
    settings,
) -> Response:
    if not auth_settings.auth_enabled or request.url.path in PUBLIC_MANAGEMENT_PATHS:
        return await call_next(request)

    token = request.cookies.get(settings.auth_cookie_name)
    if not token:
        return build_auth_error_response(
            request,
            status_code=401,
            detail="Authentication required",
        )

    try:
        token_payload = decode_access_token(token)
    except Exception:
        return build_auth_error_response(
            request,
            status_code=401,
            detail="Authentication required",
        )

    auth_subject = _get_authenticated_subject(auth_settings, token_payload)
    if auth_subject is None:
        return build_auth_error_response(
            request,
            status_code=401,
            detail="Authentication required",
        )

    request.state.auth_subject = auth_subject
    return await call_next(request)


async def _handle_proxy_authentication(
    request: Request,
    call_next: CallNext,
    *,
    auth_settings,
    session,
) -> Response:
    if not auth_settings.auth_enabled:
        return await call_next(request)

    raw_key, _ = extract_proxy_api_key(
        {key.lower(): value for key, value in request.headers.items()}
    )
    if not raw_key:
        return build_auth_error_response(
            request,
            status_code=401,
            detail="Proxy API key required",
        )

    proxy_key = await verify_proxy_api_key(session, raw_key=raw_key)
    if proxy_key is None:
        return build_auth_error_response(
            request,
            status_code=401,
            detail="Invalid proxy API key",
        )

    last_used_at = utc_now()
    last_used_ip = request.client.host if request.client else None
    request.state.proxy_api_key_id = proxy_key.id
    request.state.proxy_api_key_name = proxy_key.name
    response = await call_next(request)

    background_task_manager = getattr(
        request.app.state, "background_task_manager", None
    )
    if not enqueue_proxy_api_key_usage(
        background_task_manager,
        key_id=proxy_key.id,
        last_used_at=last_used_at,
        last_used_ip=last_used_ip,
    ):
        _append_response_background_task(
            response,
            lambda: persist_proxy_api_key_usage(
                key_id=proxy_key.id,
                last_used_at=last_used_at,
                last_used_ip=last_used_ip,
            ),
        )
    return response


async def handle_authentication(
    request: Request,
    call_next: CallNext,
    *,
    settings,
) -> Response:
    if request.method.upper() == "OPTIONS" or not _requires_auth_handling(
        request.url.path
    ):
        return await call_next(request)

    async with database_core.AsyncSessionLocal() as session:
        auth_settings = await get_app_auth_settings_snapshot(session)
        _set_request_auth_state(request, auth_enabled=auth_settings.auth_enabled)

        if request.url.path.startswith("/api/"):
            return await _handle_management_authentication(
                request,
                call_next,
                auth_settings=auth_settings,
                settings=settings,
            )

        return await _handle_proxy_authentication(
            request,
            call_next,
            auth_settings=auth_settings,
            session=session,
        )


__all__ = [
    "PUBLIC_MANAGEMENT_PATHS",
    "build_auth_error_response",
    "handle_authentication",
]
