from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from fastapi import HTTPException

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _send_smtp_message(*, message: EmailMessage, recipient: str) -> None:
    settings = get_settings()
    smtp_host = settings.smtp_host
    smtp_username = settings.smtp_username
    smtp_password = settings.smtp_password
    if not smtp_host:
        raise HTTPException(status_code=503, detail="SMTP is not configured")
    try:
        with smtplib.SMTP(smtp_host, settings.smtp_port, timeout=30) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if smtp_username and smtp_password:
                smtp.login(smtp_username, smtp_password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        logger.warning("SMTP delivery failed for %s: %s", recipient, exc)
        raise HTTPException(
            status_code=503, detail="Email service temporarily unavailable"
        ) from exc


def _build_message(*, recipient: str, subject: str, body: str) -> EmailMessage:
    settings = get_settings()
    if not settings.smtp_host or not settings.smtp_sender_email:
        raise HTTPException(status_code=503, detail="SMTP is not configured")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = (
        f"{settings.smtp_sender_name} <{settings.smtp_sender_email}>"
        if settings.smtp_sender_name
        else settings.smtp_sender_email
    )
    message["To"] = recipient
    message.set_content(body)
    return message


def send_password_reset_email(*, recipient: str, otp_code: str) -> None:
    settings = get_settings()
    message = _build_message(
        recipient=recipient,
        subject="Prism password reset code",
        body=(
            "Use this Prism password reset code to continue: "
            f"{otp_code}. The code expires in {settings.auth_reset_code_ttl_seconds // 60} minutes."
        ),
    )
    if settings.log_level.lower() == "debug":
        logger.debug("Prism password reset OTP for %s: %s", recipient, otp_code)
    _send_smtp_message(message=message, recipient=recipient)


def send_email_verification_otp(*, recipient: str, otp_code: str) -> None:
    settings = get_settings()
    message = _build_message(
        recipient=recipient,
        subject="Prism email verification code",
        body=(
            "Use this Prism verification code to bind your email: "
            f"{otp_code}. The code expires in {settings.auth_reset_code_ttl_seconds // 60} minutes."
        ),
    )
    if settings.log_level.lower() == "debug":
        logger.debug("Prism email verification OTP for %s: %s", recipient, otp_code)
    _send_smtp_message(message=message, recipient=recipient)


__all__ = ["send_email_verification_otp", "send_password_reset_email"]
