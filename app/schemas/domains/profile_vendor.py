from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .common import ApiFamily


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


class VendorBase(BaseModel):
    key: str
    name: str
    description: str | None = None
    icon_key: str | None = None


class VendorCreate(VendorBase):
    pass


class VendorUpdate(BaseModel):
    key: str | None = None
    name: str | None = None
    description: str | None = None
    icon_key: str | None = None
    audit_enabled: bool | None = None
    audit_capture_bodies: bool | None = None


class VendorResponse(VendorBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    audit_enabled: bool
    audit_capture_bodies: bool
    created_at: datetime
    updated_at: datetime


class VendorModelUsageItem(BaseModel):
    model_config_id: int
    profile_id: int
    profile_name: str
    model_id: str
    display_name: str | None
    model_type: Literal["native", "proxy"]
    api_family: ApiFamily
    is_enabled: bool


class VendorDeleteConflictDetail(BaseModel):
    message: str
    models: list[VendorModelUsageItem]


__all__ = [
    "VendorDeleteConflictDetail",
    "ProfileActivateRequest",
    "ProfileBase",
    "ProfileCreate",
    "ProfileResponse",
    "ProfileUpdate",
    "VendorBase",
    "VendorCreate",
    "VendorModelUsageItem",
    "VendorResponse",
    "VendorUpdate",
]
