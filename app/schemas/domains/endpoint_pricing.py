from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .common import _CURRENCY_CODE_RE, _validate_decimal_non_negative


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
    has_api_key: bool = False
    masked_api_key: str | None = None
    position: int
    created_at: datetime
    updated_at: datetime


class EndpointPositionMoveRequest(BaseModel):
    to_index: int = Field(ge=0)


class ConnectionPriorityMoveRequest(BaseModel):
    to_index: int = Field(ge=0)


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
    expected_updated_at: datetime
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


__all__ = [
    "ConnectionPricingTemplateSummary",
    "ConnectionPricingTemplateUpdate",
    "ConnectionPriorityMoveRequest",
    "EndpointBase",
    "EndpointCreate",
    "EndpointPositionMoveRequest",
    "EndpointResponse",
    "EndpointUpdate",
    "PricingTemplateConnectionUsageItem",
    "PricingTemplateConnectionsResponse",
    "PricingTemplateCreate",
    "PricingTemplateListItem",
    "PricingTemplateResponse",
    "PricingTemplateUpdate",
]
