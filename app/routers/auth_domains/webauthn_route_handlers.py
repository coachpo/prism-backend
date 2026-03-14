from collections.abc import Callable

from fastapi import HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.schemas import (
    WebAuthnAuthenticationOptionsResponse,
    WebAuthnAuthenticationVerifyRequest,
    WebAuthnCredentialListResponse,
    WebAuthnCredentialResponse,
    WebAuthnRegistrationOptionsResponse,
    WebAuthnRegistrationVerifyRequest,
)
from app.services import webauthn_service
from app.services.auth_service import (
    create_session_for_auth_subject,
    get_or_create_app_auth_settings,
)


async def webauthn_registration_options_response(
    auth_subject: dict,
    db: AsyncSession,
) -> WebAuthnRegistrationOptionsResponse:
    options = await webauthn_service.generate_registration_options_for_user(
        db,
        auth_subject_id=auth_subject["id"],
        username=auth_subject.get("username") or "operator",
    )
    return WebAuthnRegistrationOptionsResponse(**options)


async def webauthn_registration_verify_response(
    body: WebAuthnRegistrationVerifyRequest,
    auth_subject: dict,
    db: AsyncSession,
) -> dict[str, object]:
    try:
        credential = await webauthn_service.verify_and_save_registration(
            db,
            auth_subject_id=auth_subject["id"],
            credential=body.credential,
            device_name=body.device_name,
        )
        return {"success": True, "credential_id": credential.id}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def webauthn_authentication_options_response(
    db: AsyncSession,
    *,
    username: str | None = None,
) -> WebAuthnAuthenticationOptionsResponse:
    auth_subject_id: int | None = None
    normalized_username = username.strip() if username else None
    if normalized_username:
        settings_row = await get_or_create_app_auth_settings(db)
        if settings_row.username == normalized_username:
            auth_subject_id = settings_row.id

    options = await webauthn_service.generate_authentication_options_for_user(
        db,
        auth_subject_id=auth_subject_id,
    )
    return WebAuthnAuthenticationOptionsResponse(**options)


async def webauthn_authentication_verify_response(
    body: WebAuthnAuthenticationVerifyRequest,
    response: Response,
    request: Request,
    db: AsyncSession,
    *,
    set_auth_cookies_fn: Callable[..., None],
) -> dict[str, object]:
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

        set_auth_cookies_fn(
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


async def list_webauthn_credentials_response(
    auth_subject: dict,
    db: AsyncSession,
) -> WebAuthnCredentialListResponse:
    credentials = await webauthn_service.list_credentials_for_user(
        db,
        auth_subject_id=auth_subject["id"],
    )
    return WebAuthnCredentialListResponse(
        items=[WebAuthnCredentialResponse.model_validate(item) for item in credentials],
        total=len(credentials),
    )


async def revoke_webauthn_credential_response(
    credential_id: int,
    auth_subject: dict,
    db: AsyncSession,
) -> dict[str, bool]:
    success = await webauthn_service.revoke_credential(
        db,
        credential_id=credential_id,
        auth_subject_id=auth_subject["id"],
    )
    if not success:
        raise HTTPException(status_code=404, detail="Credential not found")
    return {"success": True}


__all__ = [
    "list_webauthn_credentials_response",
    "revoke_webauthn_credential_response",
    "webauthn_authentication_options_response",
    "webauthn_authentication_verify_response",
    "webauthn_registration_options_response",
    "webauthn_registration_verify_response",
]
