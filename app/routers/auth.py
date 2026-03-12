import asyncio
import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import RefreshSessionDuration, get_refresh_cookie_max_age
from app.dependencies import get_db, get_request_auth_subject
from app.schemas.schemas import (
    AuthStatusResponse,
    LoginRequest,
    PasswordResetConfirmRequest,
    PasswordResetConfirmResponse,
    PasswordResetRequest,
    PasswordResetRequestResponse,
    SessionResponse,
)
from app.services.auth_service import (
    authenticate_user,
    consume_password_reset_challenge,
    create_password_reset_challenge,
    get_or_create_app_auth_settings,
    revoke_refresh_token,
    rotate_refresh_token,
    send_password_reset_email,
)
from app.core.config import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger(__name__)


def _set_auth_cookies(
    response: Response,
    *,
    access_token: str,
    refresh_token: str,
    refresh_expires_at: datetime,
    session_duration: RefreshSessionDuration,
) -> None:
    settings = get_settings()
    max_age_access = get_refresh_cookie_max_age(
        session_duration=session_duration,
        expires_at=refresh_expires_at,
    )
    max_age_refresh = get_refresh_cookie_max_age(
        session_duration=session_duration,
        expires_at=refresh_expires_at,
    )
    response.set_cookie(
        settings.auth_cookie_name,
        access_token,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        max_age=max_age_access,
        path="/",
    )
    response.set_cookie(
        settings.auth_refresh_cookie_name,
        refresh_token,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        max_age=max_age_refresh,
        path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(settings.auth_cookie_name, path="/")
    response.delete_cookie(settings.auth_refresh_cookie_name, path="/")


@router.get("/status", response_model=AuthStatusResponse)
async def get_auth_status(db: Annotated[AsyncSession, Depends(get_db)]):
    settings_row = await get_or_create_app_auth_settings(db)
    return AuthStatusResponse(auth_enabled=settings_row.auth_enabled)


@router.post("/login", response_model=SessionResponse)
async def login(
    body: LoginRequest,
    response: Response,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
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
    _set_auth_cookies(
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


@router.post("/logout", response_model=SessionResponse)
async def logout(
    response: Response,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    refresh_cookie = request.cookies.get(get_settings().auth_refresh_cookie_name)
    if refresh_cookie:
        await revoke_refresh_token(db, raw_refresh_token=refresh_cookie)
    _clear_auth_cookies(response)
    settings_row = await get_or_create_app_auth_settings(db)
    return SessionResponse(
        authenticated=False, auth_enabled=settings_row.auth_enabled, username=None
    )


@router.post("/refresh", response_model=SessionResponse)
async def refresh_session(
    response: Response,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    refresh_cookie = request.cookies.get(get_settings().auth_refresh_cookie_name)
    if not refresh_cookie:
        _clear_auth_cookies(response)
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
        _clear_auth_cookies(response)
        return SessionResponse(authenticated=False, auth_enabled=True, username=None)
    _set_auth_cookies(
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


@router.get("/session", response_model=SessionResponse)
async def get_session(
    auth_subject: Annotated[dict[str, object], Depends(get_request_auth_subject)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    settings_row = await get_or_create_app_auth_settings(db)
    return SessionResponse(
        authenticated=True,
        auth_enabled=settings_row.auth_enabled,
        username=str(auth_subject.get("username") or settings_row.username or ""),
    )


@router.post("/password-reset/request", response_model=PasswordResetRequestResponse)
async def request_password_reset(
    body: PasswordResetRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    settings_row = await get_or_create_app_auth_settings(db)
    identifier = body.username_or_email.strip()
    if (
        settings_row.auth_enabled
        and settings_row.email
        and identifier
        and identifier in {settings_row.username or "", settings_row.email}
    ):
        _, otp_code = await create_password_reset_challenge(
            db,
            settings_row=settings_row,
            requested_ip=request.client.host if request.client else None,
        )
        try:
            await asyncio.to_thread(
                send_password_reset_email,
                recipient=settings_row.email,
                otp_code=otp_code,
            )
        except Exception:
            logger.exception(
                "Failed to send password reset email for auth subject %s",
                settings_row.id,
            )
    return PasswordResetRequestResponse(success=True)


@router.post("/password-reset/confirm", response_model=PasswordResetConfirmResponse)
async def confirm_password_reset(
    body: PasswordResetConfirmRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await consume_password_reset_challenge(
        db,
        otp_code=body.otp_code.strip(),
        new_password=body.new_password,
    )
    _clear_auth_cookies(response)
    return PasswordResetConfirmResponse(success=True)
