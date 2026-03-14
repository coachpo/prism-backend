from datetime import datetime

from fastapi import Response

from app.core.auth import RefreshSessionDuration, get_refresh_cookie_max_age
from app.core.config import get_settings


def set_auth_cookies(
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


def clear_auth_cookies(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(settings.auth_cookie_name, path="/")
    response.delete_cookie(settings.auth_refresh_cookie_name, path="/")


__all__ = ["clear_auth_cookies", "set_auth_cookies"]
