import json
from datetime import datetime
from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .common import AuthType
from .endpoint_pricing import (
    ConnectionPricingTemplateSummary,
    EndpointCreate,
    EndpointResponse,
)
from .profile_provider import ProviderResponse


class ConnectionBase(BaseModel):
    is_active: bool = True
    name: str | None = None
    auth_type: AuthType | None = None
    custom_headers: dict[str, str] | None = None
    pricing_template_id: int | None = None


class ConnectionCreate(ConnectionBase):
    model_config = ConfigDict(extra="forbid")

    endpoint_id: int | None = None
    endpoint_create: EndpointCreate | None = None

    @model_validator(mode="after")
    def validate_endpoint_selector(self):
        has_endpoint_id = self.endpoint_id is not None
        has_endpoint_create = self.endpoint_create is not None
        if has_endpoint_id == has_endpoint_create:
            raise ValueError(
                "Exactly one of endpoint_id or endpoint_create must be provided"
            )
        return self


class ConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_id: int | None = None
    endpoint_create: EndpointCreate | None = None
    is_active: bool | None = None
    name: str | None = None
    auth_type: AuthType | None = None
    custom_headers: dict[str, str] | None = None
    pricing_template_id: int | None = None

    @model_validator(mode="after")
    def validate_update(self):
        if self.endpoint_id is not None and self.endpoint_create is not None:
            raise ValueError("endpoint_id and endpoint_create are mutually exclusive")
        return self


class ConnectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
    model_config_id: int
    endpoint_id: int
    endpoint: EndpointResponse | None = Field(
        default=None, validation_alias=AliasChoices("endpoint", "endpoint_rel")
    )
    is_active: bool
    priority: int
    name: str | None
    auth_type: AuthType | None
    custom_headers: dict[str, str] | None
    pricing_template_id: int | None
    pricing_template: ConnectionPricingTemplateSummary | None = Field(
        default=None,
        validation_alias=AliasChoices("pricing_template", "pricing_template_rel"),
    )
    health_status: str
    health_detail: str | None
    last_health_check: datetime | None
    created_at: datetime
    updated_at: datetime

    @field_validator("custom_headers", mode="before")
    @classmethod
    def parse_custom_headers(cls, v: str | dict | None) -> dict[str, str] | None:
        if v is None:
            return None
        if isinstance(v, dict):
            return v
        return json.loads(v)


class HealthCheckResponse(BaseModel):
    connection_id: int
    health_status: str
    checked_at: datetime
    detail: str
    response_time_ms: int


class ConnectionOwnerResponse(BaseModel):
    connection_id: int
    model_config_id: int
    model_id: str
    connection_name: str | None
    endpoint_id: int
    endpoint_name: str
    endpoint_base_url: str


class ConnectionSuccessRateResponse(BaseModel):
    connection_id: int
    total_requests: int
    success_count: int
    error_count: int
    success_rate: float | None


class ModelConfigBase(BaseModel):
    provider_id: int
    model_id: str
    display_name: str | None = None
    model_type: Literal["native", "proxy"] = "native"
    redirect_to: str | None = None
    lb_strategy: Literal["single", "failover"] = "single"
    failover_recovery_enabled: bool = True
    failover_recovery_cooldown_seconds: int = 60

    @field_validator("failover_recovery_cooldown_seconds")
    @classmethod
    def validate_cooldown(cls, v: int) -> int:
        if v < 1 or v > 3600:
            raise ValueError(
                "failover_recovery_cooldown_seconds must be between 1 and 3600"
            )
        return v

    is_enabled: bool = True


class ModelConfigCreate(ModelConfigBase):
    pass


class ModelConfigUpdate(BaseModel):
    provider_id: int | None = None
    model_id: str | None = None
    display_name: str | None = None
    model_type: Literal["native", "proxy"] | None = None
    redirect_to: str | None = None
    lb_strategy: Literal["single", "failover"] | None = None
    failover_recovery_enabled: bool | None = None
    failover_recovery_cooldown_seconds: int | None = None
    is_enabled: bool | None = None


class ModelConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
    provider_id: int
    provider: ProviderResponse
    model_id: str
    display_name: str | None
    model_type: Literal["native", "proxy"]
    redirect_to: str | None
    lb_strategy: Literal["single", "failover"]
    failover_recovery_enabled: bool
    failover_recovery_cooldown_seconds: int
    is_enabled: bool
    connections: list[ConnectionResponse]
    created_at: datetime
    updated_at: datetime


class ModelConfigListResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
    provider_id: int
    provider: ProviderResponse
    model_id: str
    display_name: str | None
    model_type: Literal["native", "proxy"]
    redirect_to: str | None
    lb_strategy: Literal["single", "failover"]
    failover_recovery_enabled: bool
    failover_recovery_cooldown_seconds: int
    is_enabled: bool
    connection_count: int
    active_connection_count: int
    health_success_rate: float | None = None
    health_total_requests: int = 0
    created_at: datetime
    updated_at: datetime


class EndpointModelsBatchRequest(BaseModel):
    endpoint_ids: list[int]

    @field_validator("endpoint_ids")
    @classmethod
    def validate_endpoint_ids(cls, value: list[int]) -> list[int]:
        normalized = list(dict.fromkeys(value))
        if not normalized:
            raise ValueError("endpoint_ids must contain at least one endpoint id")
        return normalized


class EndpointModelsBatchItem(BaseModel):
    endpoint_id: int
    models: list[ModelConfigListResponse]


class EndpointModelsBatchResponse(BaseModel):
    items: list[EndpointModelsBatchItem]


__all__ = [
    "ConnectionBase",
    "ConnectionCreate",
    "ConnectionOwnerResponse",
    "ConnectionResponse",
    "ConnectionSuccessRateResponse",
    "ConnectionUpdate",
    "EndpointModelsBatchItem",
    "EndpointModelsBatchRequest",
    "EndpointModelsBatchResponse",
    "HealthCheckResponse",
    "ModelConfigBase",
    "ModelConfigCreate",
    "ModelConfigListResponse",
    "ModelConfigResponse",
    "ModelConfigUpdate",
]
