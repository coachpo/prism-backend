import re
from decimal import Decimal, InvalidOperation
from datetime import datetime
from typing import Literal
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator
import json

_HEADER_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")
_CURRENCY_CODE_RE = re.compile(r"^[A-Z]{3}$")


def _validate_decimal_non_negative(value: str | None, field_name: str) -> str | None:
    if value is None or value == "":
        return value
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid decimal") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return f"{parsed}"


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


AuthType = Literal["openai", "anthropic", "gemini"]


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


# --- Global Endpoint Schemas (credentials only) ---


class EndpointBase(BaseModel):
    name: str
    base_url: str
    api_key: str


class EndpointCreate(EndpointBase):
    pass


class EndpointUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class EndpointResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
    name: str
    base_url: str
    api_key: str
    created_at: datetime
    updated_at: datetime


# --- Pricing Template + Connection Schemas ---


class PricingTemplateCreate(BaseModel):
    name: str
    description: str | None = None
    pricing_unit: Literal["PER_1M"] = "PER_1M"
    pricing_currency_code: str
    input_price: str
    output_price: str
    cached_input_price: str | None = None
    cache_creation_price: str | None = None
    reasoning_price: str | None = None
    missing_special_token_price_policy: Literal["MAP_TO_OUTPUT", "ZERO_COST"] = (
        "MAP_TO_OUTPUT"
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("name must not be empty")
        if len(trimmed) > 200:
            raise ValueError("name must be at most 200 characters")
        return trimmed

    @field_validator("description")
    @classmethod
    def validate_description(cls, v: str | None) -> str | None:
        if v is None:
            return None
        trimmed = v.strip()
        return trimmed or None

    @field_validator(
        "input_price",
        "output_price",
        "cached_input_price",
        "cache_creation_price",
        "reasoning_price",
        mode="before",
    )
    @classmethod
    def validate_prices(cls, v: str | int | float | Decimal | None, info) -> str | None:
        if v is None:
            return None
        return _validate_decimal_non_negative(str(v), info.field_name)

    @field_validator("pricing_currency_code")
    @classmethod
    def validate_currency_code(cls, v: str) -> str:
        code = v.strip().upper()
        if not _CURRENCY_CODE_RE.match(code):
            raise ValueError(
                "pricing_currency_code must be a 3-letter uppercase ISO code"
            )
        return code


class PricingTemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    pricing_unit: Literal["PER_1M"] | None = None
    pricing_currency_code: str | None = None
    input_price: str | None = None
    output_price: str | None = None
    cached_input_price: str | None = None
    cache_creation_price: str | None = None
    reasoning_price: str | None = None
    missing_special_token_price_policy: Literal["MAP_TO_OUTPUT", "ZERO_COST"] | None = (
        None
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("name must not be empty")
        if len(trimmed) > 200:
            raise ValueError("name must be at most 200 characters")
        return trimmed

    @field_validator("description")
    @classmethod
    def validate_description(cls, v: str | None) -> str | None:
        if v is None:
            return None
        trimmed = v.strip()
        return trimmed or None

    @field_validator(
        "input_price",
        "output_price",
        "cached_input_price",
        "cache_creation_price",
        "reasoning_price",
        mode="before",
    )
    @classmethod
    def validate_prices(cls, v: str | int | float | Decimal | None, info) -> str | None:
        if v is None:
            return None
        return _validate_decimal_non_negative(str(v), info.field_name)

    @field_validator("pricing_currency_code")
    @classmethod
    def validate_currency_code(cls, v: str | None) -> str | None:
        if v is None:
            return None
        code = v.strip().upper()
        if not _CURRENCY_CODE_RE.match(code):
            raise ValueError(
                "pricing_currency_code must be a 3-letter uppercase ISO code"
            )
        return code


class PricingTemplateListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
    name: str
    description: str | None
    pricing_unit: Literal["PER_1M"]
    pricing_currency_code: str
    version: int
    updated_at: datetime


class PricingTemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
    name: str
    description: str | None
    pricing_unit: Literal["PER_1M"]
    pricing_currency_code: str
    input_price: str
    output_price: str
    cached_input_price: str | None
    cache_creation_price: str | None
    reasoning_price: str | None
    missing_special_token_price_policy: Literal["MAP_TO_OUTPUT", "ZERO_COST"]
    version: int
    created_at: datetime
    updated_at: datetime


class PricingTemplateConnectionUsageItem(BaseModel):
    connection_id: int
    connection_name: str | None
    model_config_id: int
    model_id: str
    endpoint_id: int
    endpoint_name: str


class PricingTemplateConnectionsResponse(BaseModel):
    template_id: int
    items: list[PricingTemplateConnectionUsageItem]


class ConnectionPricingTemplateUpdate(BaseModel):
    pricing_template_id: int | None = None


class ConnectionPricingTemplateSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    pricing_unit: Literal["PER_1M"]
    pricing_currency_code: str
    version: int


class ConnectionBase(BaseModel):
    is_active: bool = True
    priority: int = 0
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
    priority: int | None = None
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
    endpoint: EndpointResponse | None = Field(default=None, validation_alias=AliasChoices("endpoint", "endpoint_rel"))
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


# --- Model Config Schemas ---


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
