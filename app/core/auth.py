from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

import jwt

from app.core.config import get_settings
from app.core.crypto import hash_opaque_token

PROXY_API_KEY_PREFIX = "pm-"
PROXY_API_KEY_LOOKUP_LENGTH = 8
REFRESH_SESSION_DURATION_VALUES = ("session", "7_days", "30_days")
RefreshSessionDuration = Literal["session", "7_days", "30_days"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(*, subject_id: int, username: str, token_version: int) -> str:
    settings = get_settings()
    now = utc_now()
    payload = {
        "sub": str(subject_id),
        "username": username,
        "token_version": token_version,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int(
            (
                now + timedelta(seconds=settings.auth_access_token_ttl_seconds)
            ).timestamp()
        ),
        "jti": str(uuid4()),
    }
    return jwt.encode(payload, settings.auth_jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict[str, object]:
    settings = get_settings()
    payload = jwt.decode(token, settings.auth_jwt_secret, algorithms=["HS256"])
    token_type = payload.get("type")
    if token_type != "access":
        raise jwt.InvalidTokenError("Invalid token type")
    return payload


def build_refresh_token_record(*, expires_at: datetime) -> tuple[str, str, datetime]:
    raw_token = secrets.token_urlsafe(48)
    return raw_token, hash_opaque_token(raw_token), expires_at


def get_refresh_token_expiry(*, session_duration: RefreshSessionDuration) -> datetime:
    now = utc_now()
    if session_duration == "session":
        settings = get_settings()
        return now + timedelta(seconds=settings.auth_refresh_token_ttl_seconds)
    if session_duration == "7_days":
        return now + timedelta(days=7)
    return now + timedelta(days=30)


def get_refresh_cookie_max_age(
    *, session_duration: RefreshSessionDuration, expires_at: datetime
) -> int | None:
    if session_duration == "session":
        return None
    remaining_seconds = int((expires_at - utc_now()).total_seconds())
    return max(0, remaining_seconds)


def normalize_refresh_session_duration(session_duration: str) -> RefreshSessionDuration:
    if session_duration == "session":
        return "session"
    if session_duration == "7_days":
        return "7_days"
    if session_duration == "30_days":
        return "30_days"
    raise ValueError("Invalid refresh session duration")


def build_proxy_api_key(prefix: str = PROXY_API_KEY_PREFIX) -> tuple[str, str, str]:
    lookup = secrets.token_hex(PROXY_API_KEY_LOOKUP_LENGTH // 2)
    secret = secrets.token_hex(12)
    key_prefix = f"{prefix}{lookup}"
    raw_key = f"{key_prefix}{secret}"
    return raw_key, key_prefix, raw_key[-4:]


def parse_proxy_api_key(raw_key: str) -> tuple[str, str]:
    normalized = raw_key.strip()
    prefix_length = len(PROXY_API_KEY_PREFIX) + PROXY_API_KEY_LOOKUP_LENGTH
    if normalized.startswith(PROXY_API_KEY_PREFIX) and len(normalized) > prefix_length:
        return normalized, normalized[:prefix_length]
    if "_" in normalized:
        compatible_prefix, _ = normalized.rsplit("_", 1)
        return normalized, compatible_prefix
    raise ValueError("Invalid proxy API key format")


def extract_proxy_api_key(
    headers: dict[str, str],
) -> tuple[str | None, Literal["authorization", "x-api-key", "x-goog-api-key"] | None]:
    authorization = headers.get("authorization")
    if authorization:
        parts = authorization.strip().split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
            return parts[1].strip(), "authorization"
    for header_name in ("x-api-key", "x-goog-api-key"):
        value = headers.get(header_name)
        if value and value.strip():
            return value.strip(), header_name
    return None, None
