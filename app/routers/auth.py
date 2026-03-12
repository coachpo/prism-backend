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
    WebAuthnRegistrationOptionsResponse,
    WebAuthnRegistrationVerifyRequest,
    WebAuthnAuthenticationOptionsResponse,
    WebAuthnAuthenticationVerifyRequest,
    WebAuthnCredentialResponse,
    WebAuthnCredentialListResponse,
)
from app.services.auth_service import (
    authenticate_user,
    consume_password_reset_challenge,
    create_session_for_auth_subject,
    create_password_reset_challenge,
    get_or_create_app_auth_settings,
    revoke_refresh_token,
    rotate_refresh_token,
    send_password_reset_email,
)
from app.services import webauthn_service
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


# --- WebAuthn / Passkey Endpoints ---


@router.post(
    "/webauthn/register/options", response_model=WebAuthnRegistrationOptionsResponse
)
async def webauthn_registration_options(
    auth_subject: Annotated[dict, Depends(get_request_auth_subject)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Generate WebAuthn registration options for authenticated user."""
    auth_subject_id = auth_subject["id"]
    username = auth_subject.get("username") or "operator"

    options = await webauthn_service.generate_registration_options_for_user(
        db, auth_subject_id=auth_subject_id, username=username
    )
    return WebAuthnRegistrationOptionsResponse(**options)


@router.post("/webauthn/register/verify")
async def webauthn_registration_verify(
    body: WebAuthnRegistrationVerifyRequest,
    auth_subject: Annotated[dict, Depends(get_request_auth_subject)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Verify and save WebAuthn registration."""
    auth_subject_id = auth_subject["id"]

    try:
        credential = await webauthn_service.verify_and_save_registration(
            db,
            auth_subject_id=auth_subject_id,
            credential=body.credential,
            device_name=body.device_name,
        )
        return {"success": True, "credential_id": credential.id}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/webauthn/authenticate/options",
    response_model=WebAuthnAuthenticationOptionsResponse,
)
async def webauthn_authentication_options(
    db: Annotated[AsyncSession, Depends(get_db)],
    username: str | None = None,
):
    """Generate WebAuthn authentication options."""
    auth_subject_id: int | None = None
    normalized_username = username.strip() if username else None
    if normalized_username:
        settings_row = await get_or_create_app_auth_settings(db)
        if settings_row.username == normalized_username:
            auth_subject_id = settings_row.id

    options = await webauthn_service.generate_authentication_options_for_user(
        db, auth_subject_id=auth_subject_id
    )
    return WebAuthnAuthenticationOptionsResponse(**options)


@router.post("/webauthn/authenticate/verify")
async def webauthn_authentication_verify(
    body: WebAuthnAuthenticationVerifyRequest,
    response: Response,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Verify WebAuthn authentication and create session."""
    try:
        _, auth_subject_id = await webauthn_service.verify_authentication(
            db,
            credential=body.credential,
            client_ip=request.client.host if request.client else None,
        )
        (
            settings_row,
            access_token,
            refresh_token,
            refresh_expires_at,
            session_duration,
        ) = await create_session_for_auth_subject(
            db,
            auth_subject_id=auth_subject_id,
            session_duration="7_days",
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

        return {
            "success": True,
            "authenticated": True,
            "username": settings_row.username,
        }
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.get("/webauthn/credentials", response_model=WebAuthnCredentialListResponse)
async def list_webauthn_credentials(
    auth_subject: Annotated[dict, Depends(get_request_auth_subject)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List user's WebAuthn credentials."""
    auth_subject_id = auth_subject["id"]
    credentials = await webauthn_service.list_credentials_for_user(
        db, auth_subject_id=auth_subject_id
    )
    return WebAuthnCredentialListResponse(
        items=[WebAuthnCredentialResponse.model_validate(c) for c in credentials],
        total=len(credentials),
    )


@router.delete("/webauthn/credentials/{credential_id}")
async def revoke_webauthn_credential(
    credential_id: int,
    auth_subject: Annotated[dict, Depends(get_request_auth_subject)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Revoke a WebAuthn credential."""
    auth_subject_id = auth_subject["id"]

    success = await webauthn_service.revoke_credential(
        db, credential_id=credential_id, auth_subject_id=auth_subject_id
    )

    if not success:
        raise HTTPException(status_code=404, detail="Credential not found")

    return {"success": True}
