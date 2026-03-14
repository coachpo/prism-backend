from collections.abc import Callable

from fastapi import HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.schemas.schemas import AuthStatusResponse, LoginRequest, SessionResponse
from app.services.auth_service import (
    authenticate_user,
    get_or_create_app_auth_settings,
    revoke_refresh_token,
    rotate_refresh_token,
)


async def get_auth_status_response(db: AsyncSession) -> AuthStatusResponse:
    settings_row = await get_or_create_app_auth_settings(db)
    return AuthStatusResponse(auth_enabled=settings_row.auth_enabled)


async def login_response(
    body: LoginRequest,
    response: Response,
    request: Request,
    db: AsyncSession,
    *,
    set_auth_cookies_fn: Callable[..., None],
) -> SessionResponse:
    (
        settings_row,
        access_token,
        refresh_token,
        refresh_expires_at,
        session_duration,
    ) = await authenticate_user(
        db,
        username=body.username.strip(),
        password=body.password,
        session_duration=body.session_duration,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    set_auth_cookies_fn(
        response,
        access_token=access_token,
        refresh_token=refresh_token,
        refresh_expires_at=refresh_expires_at,
        session_duration=session_duration,
    )
    return SessionResponse(
        authenticated=True,
        auth_enabled=settings_row.auth_enabled,
        username=settings_row.username,
    )


async def logout_response(
    response: Response,
    request: Request,
    db: AsyncSession,
    *,
    clear_auth_cookies_fn: Callable[[Response], None],
) -> SessionResponse:
    refresh_cookie = request.cookies.get(get_settings().auth_refresh_cookie_name)
    if refresh_cookie:
        await revoke_refresh_token(db, raw_refresh_token=refresh_cookie)
    clear_auth_cookies_fn(response)
    settings_row = await get_or_create_app_auth_settings(db)
    return SessionResponse(
        authenticated=False,
        auth_enabled=settings_row.auth_enabled,
        username=None,
    )


async def refresh_session_response(
    response: Response,
    request: Request,
    db: AsyncSession,
    *,
    clear_auth_cookies_fn: Callable[[Response], None],
    set_auth_cookies_fn: Callable[..., None],
) -> SessionResponse:
    refresh_cookie = request.cookies.get(get_settings().auth_refresh_cookie_name)
    if not refresh_cookie:
        clear_auth_cookies_fn(response)
        return SessionResponse(authenticated=False, auth_enabled=True, username=None)

    try:
        (
            settings_row,
            access_token,
            new_refresh_token,
            refresh_expires_at,
            session_duration,
        ) = await rotate_refresh_token(
            db,
            raw_refresh_token=refresh_cookie,
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        clear_auth_cookies_fn(response)
        return SessionResponse(authenticated=False, auth_enabled=True, username=None)

    set_auth_cookies_fn(
        response,
        access_token=access_token,
        refresh_token=new_refresh_token,
        refresh_expires_at=refresh_expires_at,
        session_duration=session_duration,
    )
    return SessionResponse(
        authenticated=True,
        auth_enabled=settings_row.auth_enabled,
        username=settings_row.username,
    )


async def get_session_response(
    auth_subject: dict[str, object],
    db: AsyncSession,
) -> SessionResponse:
    settings_row = await get_or_create_app_auth_settings(db)
    return SessionResponse(
        authenticated=True,
        auth_enabled=settings_row.auth_enabled,
        username=str(auth_subject.get("username") or settings_row.username or ""),
    )


__all__ = [
    "get_auth_status_response",
    "get_session_response",
    "login_response",
    "logout_response",
    "refresh_session_response",
]
