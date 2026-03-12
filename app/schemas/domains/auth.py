from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


LoginSessionDuration = Literal["session", "7_days", "30_days"]


class AuthStatusResponse(BaseModel):
    auth_enabled: bool


class AuthSettingsResponse(BaseModel):
    auth_enabled: bool
    username: str | None
    email: str | None
    email_bound_at: datetime | None
    pending_email: str | None = None
    email_verification_required: bool = False
    has_password: bool
    proxy_key_limit: int = 10


class AuthSettingsUpdate(BaseModel):
    auth_enabled: bool
    username: str | None = None
    password: str | None = None

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            return None
        if len(trimmed) > 200:
            raise ValueError("username must be at most 200 characters")
        return trimmed

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) < 8:
            raise ValueError("password must be at least 8 characters")
        if len(value) > 512:
            raise ValueError("password must be at most 512 characters")
        return value

    @model_validator(mode="after")
    def validate_payload(self):
        if self.auth_enabled:
            if not self.username:
                raise ValueError("username is required when enabling authentication")
        return self


class LoginRequest(BaseModel):
    username: str
    password: str
    session_duration: LoginSessionDuration = "7_days"


class SessionResponse(BaseModel):
    authenticated: bool
    auth_enabled: bool
    username: str | None = None


class PasswordResetRequest(BaseModel):
    username_or_email: str


class PasswordResetConfirmRequest(BaseModel):
    otp_code: str = Field(min_length=6, max_length=32)
    new_password: str = Field(min_length=8, max_length=512)


class PasswordResetRequestResponse(BaseModel):
    success: bool


class PasswordResetConfirmResponse(BaseModel):
    success: bool


class EmailVerificationRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        trimmed = value.strip()
        if "@" not in trimmed or trimmed.startswith("@") or trimmed.endswith("@"):
            raise ValueError("email must be valid")
        if len(trimmed) > 320:
            raise ValueError("email must be at most 320 characters")
        return trimmed


class EmailVerificationConfirmRequest(BaseModel):
    otp_code: str = Field(min_length=6, max_length=32)


class EmailVerificationResponse(BaseModel):
    success: bool
    pending_email: str | None = None
    email: str | None = None
    email_bound_at: datetime | None = None


class ProxyApiKeyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    key_prefix: str
    key_preview: str
    is_active: bool
    expires_at: datetime | None
    last_used_at: datetime | None
    last_used_ip: str | None
    notes: str | None
    rotated_from_id: int | None
    created_at: datetime
    updated_at: datetime


class ProxyApiKeyCreate(BaseModel):
    name: str
    notes: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("name must not be empty")
        if len(trimmed) > 200:
            raise ValueError("name must be at most 200 characters")
        return trimmed


class ProxyApiKeyCreateResponse(BaseModel):
    key: str
    item: ProxyApiKeyResponse


class ProxyApiKeyRotateResponse(BaseModel):
    key: str
    item: ProxyApiKeyResponse


# --- WebAuthn / Passkey Schemas ---


class WebAuthnRegistrationOptionsResponse(BaseModel):
    """WebAuthn registration options for client."""
    challenge: str
    rp: dict[str, str]
    user: dict[str, str]
    pubKeyCredParams: list[dict[str, Any]]
    timeout: int
    excludeCredentials: list[dict[str, Any]]
    authenticatorSelection: dict[str, str]
    attestation: str


class WebAuthnRegistrationVerifyRequest(BaseModel):
    """Client registration response for verification."""
    credential: dict[str, Any]
    device_name: str | None = None

    @field_validator("device_name")
    @classmethod
    def validate_device_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            return None
        if len(trimmed) > 200:
            raise ValueError("device_name must be at most 200 characters")
        return trimmed


class WebAuthnAuthenticationOptionsResponse(BaseModel):
    """WebAuthn authentication options for client."""
    challenge: str
    timeout: int
    rpId: str
    allowCredentials: list[dict[str, Any]]
    userVerification: str


class WebAuthnAuthenticationVerifyRequest(BaseModel):
    """Client authentication response for verification."""
    credential: dict[str, Any]


class WebAuthnCredentialResponse(BaseModel):
    """WebAuthn credential info for management UI."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_name: str | None
    backup_eligible: bool | None
    backup_state: bool | None
    last_used_at: datetime | None
    created_at: datetime


class WebAuthnCredentialListResponse(BaseModel):
    """List of user's WebAuthn credentials."""
    items: list[WebAuthnCredentialResponse]
    total: int
