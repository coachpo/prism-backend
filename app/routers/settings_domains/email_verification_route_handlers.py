import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.schemas import (
    EmailVerificationConfirmRequest,
    EmailVerificationRequest,
    EmailVerificationResponse,
)
from app.services.auth_service import (
    begin_email_verification,
    confirm_email_verification,
    get_or_create_app_auth_settings,
    send_email_verification_otp,
)

router = APIRouter()


def _build_email_verification_response(
    *,
    pending_email: str | None,
    email: str | None,
    email_bound_at,
) -> EmailVerificationResponse:
    return EmailVerificationResponse(
        success=True,
        pending_email=pending_email,
        email=email,
        email_bound_at=email_bound_at,
    )


@router.post(
    "/auth/email-verification/request", response_model=EmailVerificationResponse
)
async def post_email_verification_request(
    body: EmailVerificationRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    settings_row = await get_or_create_app_auth_settings(db)
    updated, otp_code = await begin_email_verification(
        db,
        settings_row=settings_row,
        email=body.email,
    )
    await asyncio.to_thread(
        send_email_verification_otp,
        recipient=body.email,
        otp_code=otp_code,
    )
    return _build_email_verification_response(
        pending_email=updated.pending_email,
        email=updated.email,
        email_bound_at=updated.email_bound_at,
    )


@router.post(
    "/auth/email-verification/confirm", response_model=EmailVerificationResponse
)
async def post_email_verification_confirm(
    body: EmailVerificationConfirmRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    settings_row = await get_or_create_app_auth_settings(db)
    updated = await confirm_email_verification(
        db,
        settings_row=settings_row,
        otp_code=body.otp_code.strip(),
    )
    return _build_email_verification_response(
        pending_email=updated.pending_email,
        email=updated.email,
        email_bound_at=updated.email_bound_at,
    )


__all__ = [
    "post_email_verification_confirm",
    "post_email_verification_request",
    "router",
]
