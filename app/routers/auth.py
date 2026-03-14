from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_request_auth_subject
from app.routers.auth_domains import (
    clear_auth_cookies,
    confirm_password_reset_response,
    get_auth_status_response,
    get_public_bootstrap_response,
    get_session_response,
    list_webauthn_credentials_response,
    login_response,
    logout_response,
    refresh_session_response,
    request_password_reset_response,
    revoke_webauthn_credential_response,
    set_auth_cookies,
    webauthn_authentication_options_response,
    webauthn_authentication_verify_response,
    webauthn_registration_options_response,
    webauthn_registration_verify_response,
)
from app.schemas.schemas import (
    AuthStatusResponse,
    LoginRequest,
    PasswordResetConfirmRequest,
    PasswordResetConfirmResponse,
    PasswordResetRequest,
    PasswordResetRequestResponse,
    SessionResponse,
    WebAuthnAuthenticationOptionsResponse,
    WebAuthnAuthenticationVerifyRequest,
    WebAuthnCredentialListResponse,
    WebAuthnRegistrationOptionsResponse,
    WebAuthnRegistrationVerifyRequest,
)
from app.services.auth_service import send_password_reset_email

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _set_auth_cookies(*args, **kwargs) -> None:
    set_auth_cookies(*args, **kwargs)


def _clear_auth_cookies(response: Response) -> None:
    clear_auth_cookies(response)


@router.get("/status", response_model=AuthStatusResponse)
async def get_auth_status(db: Annotated[AsyncSession, Depends(get_db)]):
    return await get_auth_status_response(db)


@router.get("/public-bootstrap", response_model=SessionResponse)
async def get_public_bootstrap(
    response: Response,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await get_public_bootstrap_response(
        response,
        request,
        db,
        clear_auth_cookies_fn=_clear_auth_cookies,
        set_auth_cookies_fn=_set_auth_cookies,
    )


@router.post("/login", response_model=SessionResponse)
async def login(
    body: LoginRequest,
    response: Response,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await login_response(
        body,
        response,
        request,
        db,
        set_auth_cookies_fn=_set_auth_cookies,
    )


@router.post("/logout", response_model=SessionResponse)
async def logout(
    response: Response,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await logout_response(
        response,
        request,
        db,
        clear_auth_cookies_fn=_clear_auth_cookies,
    )


@router.post("/refresh", response_model=SessionResponse)
async def refresh_session(
    response: Response,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await refresh_session_response(
        response,
        request,
        db,
        clear_auth_cookies_fn=_clear_auth_cookies,
        set_auth_cookies_fn=_set_auth_cookies,
    )


@router.get("/session", response_model=SessionResponse)
async def get_session(
    auth_subject: Annotated[dict[str, object], Depends(get_request_auth_subject)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await get_session_response(auth_subject, db)


@router.post("/password-reset/request", response_model=PasswordResetRequestResponse)
async def request_password_reset(
    body: PasswordResetRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await request_password_reset_response(
        body,
        request,
        db,
        send_password_reset_email_fn=send_password_reset_email,
    )


@router.post("/password-reset/confirm", response_model=PasswordResetConfirmResponse)
async def confirm_password_reset(
    body: PasswordResetConfirmRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await confirm_password_reset_response(
        body,
        response,
        db,
        clear_auth_cookies_fn=_clear_auth_cookies,
    )


@router.post(
    "/webauthn/register/options",
    response_model=WebAuthnRegistrationOptionsResponse,
)
async def webauthn_registration_options(
    auth_subject: Annotated[dict, Depends(get_request_auth_subject)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await webauthn_registration_options_response(auth_subject, db)


@router.post("/webauthn/register/verify")
async def webauthn_registration_verify(
    body: WebAuthnRegistrationVerifyRequest,
    auth_subject: Annotated[dict, Depends(get_request_auth_subject)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await webauthn_registration_verify_response(body, auth_subject, db)


@router.post(
    "/webauthn/authenticate/options",
    response_model=WebAuthnAuthenticationOptionsResponse,
)
async def webauthn_authentication_options(
    db: Annotated[AsyncSession, Depends(get_db)],
    username: str | None = None,
):
    return await webauthn_authentication_options_response(db, username=username)


@router.post("/webauthn/authenticate/verify")
async def webauthn_authentication_verify(
    body: WebAuthnAuthenticationVerifyRequest,
    response: Response,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await webauthn_authentication_verify_response(
        body,
        response,
        request,
        db,
        set_auth_cookies_fn=_set_auth_cookies,
    )


@router.get("/webauthn/credentials", response_model=WebAuthnCredentialListResponse)
async def list_webauthn_credentials(
    auth_subject: Annotated[dict, Depends(get_request_auth_subject)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await list_webauthn_credentials_response(auth_subject, db)


@router.delete("/webauthn/credentials/{credential_id}")
async def revoke_webauthn_credential(
    credential_id: int,
    auth_subject: Annotated[dict, Depends(get_request_auth_subject)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await revoke_webauthn_credential_response(credential_id, auth_subject, db)
