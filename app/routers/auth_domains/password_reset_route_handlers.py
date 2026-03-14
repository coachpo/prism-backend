import asyncio
import logging
from collections.abc import Callable

from fastapi import Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.schemas import (
    PasswordResetConfirmRequest,
    PasswordResetConfirmResponse,
    PasswordResetRequest,
    PasswordResetRequestResponse,
)
from app.services.auth_service import (
    consume_password_reset_challenge,
    create_password_reset_challenge,
    get_or_create_app_auth_settings,
)

logger = logging.getLogger(__name__)


async def request_password_reset_response(
    body: PasswordResetRequest,
    request: Request,
    db: AsyncSession,
    *,
    send_password_reset_email_fn: Callable[..., None],
) -> PasswordResetRequestResponse:
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
                send_password_reset_email_fn,
                recipient=settings_row.email,
                otp_code=otp_code,
            )
        except Exception:
            logger.exception(
                "Failed to send password reset email for auth subject %s",
                settings_row.id,
            )
    return PasswordResetRequestResponse(success=True)


async def confirm_password_reset_response(
    body: PasswordResetConfirmRequest,
    response: Response,
    db: AsyncSession,
    *,
    clear_auth_cookies_fn: Callable[[Response], None],
) -> PasswordResetConfirmResponse:
    await consume_password_reset_challenge(
        db,
        otp_code=body.otp_code.strip(),
        new_password=body.new_password,
    )
    clear_auth_cookies_fn(response)
    return PasswordResetConfirmResponse(success=True)


__all__ = [
    "confirm_password_reset_response",
    "request_password_reset_response",
]
