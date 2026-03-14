from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ProfileBase(BaseModel):
    name: str
    description: str | None = None


class ProfileCreate(ProfileBase):
    pass


class ProfileUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class ProfileResponse(ProfileBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_active: bool
    is_default: bool
    is_editable: bool
    version: int
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ProfileActivateRequest(BaseModel):
    expected_active_profile_id: int


class ProviderBase(BaseModel):
    name: str
    provider_type: str
    description: str | None = None


class ProviderCreate(ProviderBase):
    pass


class ProviderUpdate(BaseModel):
    audit_enabled: bool | None = None
    audit_capture_bodies: bool | None = None


class ProviderResponse(ProviderBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    audit_enabled: bool
    audit_capture_bodies: bool
    created_at: datetime
    updated_at: datetime


__all__ = [
    "ProfileActivateRequest",
    "ProfileBase",
    "ProfileCreate",
    "ProfileResponse",
    "ProfileUpdate",
    "ProviderBase",
    "ProviderCreate",
    "ProviderResponse",
    "ProviderUpdate",
]
