import re
from decimal import Decimal, InvalidOperation
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
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


# --- Provider Schemas ---


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
    name: str
    base_url: str
    api_key: str
    created_at: datetime
    updated_at: datetime


# --- Connection Schemas (model-scoped routing config) ---


class ConnectionBase(BaseModel):
    is_active: bool = True
    priority: int = 0
    name: str | None = None
    description: str | None = None
    auth_type: str | None = None
    custom_headers: dict[str, str] | None = None
    pricing_enabled: bool = False
    pricing_currency_code: str | None = None
    input_price: str | None = None
    output_price: str | None = None
    cached_input_price: str | None = None
    cache_creation_price: str | None = None
    reasoning_price: str | None = None
    missing_special_token_price_policy: Literal["MAP_TO_OUTPUT", "ZERO_COST"] = (
        "MAP_TO_OUTPUT"
    )
    forward_stream_options: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_connection_name_fields(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data

        normalized = dict(data)
        if normalized.get("name") is None and normalized.get("description") is not None:
            normalized["name"] = normalized["description"]
        if (
            normalized.get("description") is None
            and normalized.get("name") is not None
        ):
            normalized["description"] = normalized["name"]
        return normalized

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

    @model_validator(mode="after")
    def validate_pricing_config(self):
        if self.pricing_enabled:
            if not self.pricing_currency_code:
                raise ValueError(
                    "pricing_currency_code is required when pricing_enabled is true"
                )
        return self

class ConnectionCreate(ConnectionBase):
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
    endpoint_id: int | None = None
    endpoint_create: EndpointCreate | None = None
    is_active: bool | None = None
    priority: int | None = None
    name: str | None = None
    description: str | None = None
    auth_type: str | None = None
    custom_headers: dict[str, str] | None = None
    pricing_enabled: bool | None = None
    pricing_currency_code: str | None = None
    input_price: str | None = None
    output_price: str | None = None
    cached_input_price: str | None = None
    cache_creation_price: str | None = None
    reasoning_price: str | None = None
    missing_special_token_price_policy: Literal["MAP_TO_OUTPUT", "ZERO_COST"] | None = (
        None
    )
    forward_stream_options: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_connection_name_fields(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data

        normalized = dict(data)
        if normalized.get("name") is None and normalized.get("description") is not None:
            normalized["name"] = normalized["description"]
        if (
            normalized.get("description") is None
            and normalized.get("name") is not None
        ):
            normalized["description"] = normalized["name"]
        return normalized

    @field_validator(
        "input_price",
        "output_price",
        "cached_input_price",
        "cache_creation_price",
        "reasoning_price",
        mode="before",
    )
    @classmethod
    def validate_update_prices(
        cls, v: str | int | float | Decimal | None, info
    ) -> str | None:
        if v is None:
            return None
        return _validate_decimal_non_negative(str(v), info.field_name)

    @field_validator("pricing_currency_code")
    @classmethod
    def validate_update_currency_code(cls, v: str | None) -> str | None:
        if v is None:
            return None
        code = v.strip().upper()
        if not _CURRENCY_CODE_RE.match(code):
            raise ValueError(
                "pricing_currency_code must be a 3-letter uppercase ISO code"
            )
        return code

    @model_validator(mode="after")
    def validate_update(self):
        if self.endpoint_id is not None and self.endpoint_create is not None:
            raise ValueError("endpoint_id and endpoint_create are mutually exclusive")
        if self.pricing_enabled is True and not self.pricing_currency_code:
            raise ValueError(
                "pricing_currency_code is required when pricing_enabled is true"
            )
        return self

class ConnectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    model_config_id: int
    endpoint_id: int
    endpoint: EndpointResponse | None = None
    is_active: bool
    priority: int
    name: str | None
    description: str | None
    auth_type: str | None
    custom_headers: dict[str, str] | None
    pricing_enabled: bool
    pricing_currency_code: str | None
    input_price: str | None
    output_price: str | None
    cached_input_price: str | None
    cache_creation_price: str | None
    reasoning_price: str | None
    missing_special_token_price_policy: Literal["MAP_TO_OUTPUT", "ZERO_COST"]
    pricing_config_version: int
    forward_stream_options: bool
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
    connection_description: str | None
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
    model_type: str = "native"
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
    model_type: str | None = None
    redirect_to: str | None = None
    lb_strategy: Literal["single", "failover"] | None = None
    failover_recovery_enabled: bool | None = None
    failover_recovery_cooldown_seconds: int | None = None
    is_enabled: bool | None = None


class ModelConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider_id: int
    provider: ProviderResponse
    model_id: str
    display_name: str | None
    model_type: str
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
    provider_id: int
    provider: ProviderResponse
    model_id: str
    display_name: str | None
    model_type: str
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


# --- Statistics Schemas ---


class RequestLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    model_id: str
    provider_type: str
    endpoint_id: int | None
    connection_id: int | None
    endpoint_base_url: str | None
    status_code: int
    response_time_ms: int
    is_stream: bool
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    success_flag: bool | None = None
    billable_flag: bool | None = None
    priced_flag: bool | None = None
    unpriced_reason: str | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    input_cost_micros: int | None = None
    output_cost_micros: int | None = None
    cache_read_input_cost_micros: int | None = None
    cache_creation_input_cost_micros: int | None = None
    reasoning_cost_micros: int | None = None
    total_cost_original_micros: int | None = None
    total_cost_user_currency_micros: int | None = None
    currency_code_original: str | None = None
    report_currency_code: str | None = None
    report_currency_symbol: str | None = None
    fx_rate_used: str | None = None
    fx_rate_source: str | None = None
    pricing_snapshot_unit: str | None = None
    pricing_snapshot_input: str | None = None
    pricing_snapshot_output: str | None = None
    pricing_snapshot_cache_read_input: str | None = None
    pricing_snapshot_cache_creation_input: str | None = None
    pricing_snapshot_reasoning: str | None = None
    pricing_snapshot_missing_special_token_price_policy: str | None = None
    pricing_config_version_used: int | None = None
    request_path: str
    error_detail: str | None
    endpoint_description: str | None = None
    created_at: datetime


class RequestLogListResponse(BaseModel):
    items: list[RequestLogResponse]
    total: int
    limit: int
    offset: int


class StatGroupResponse(BaseModel):
    key: str
    total_requests: int
    success_count: int
    error_count: int
    avg_response_time_ms: float
    total_tokens: int


class StatsSummaryResponse(BaseModel):
    total_requests: int
    success_count: int
    error_count: int
    success_rate: float
    avg_response_time_ms: float
    p95_response_time_ms: int
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    groups: list[StatGroupResponse]


class EndpointSuccessRateResponse(BaseModel):
    endpoint_id: int
    total_requests: int
    success_count: int
    error_count: int
    success_rate: float | None


class EndpointFxMapping(BaseModel):
    model_id: str
    endpoint_id: int
    fx_rate: str

    @field_validator("fx_rate", mode="before")
    @classmethod
    def validate_fx_rate(cls, v: str | Decimal | float | int) -> str:
        try:
            parsed = Decimal(str(v))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("fx_rate must be a valid decimal") from exc
        if parsed <= 0:
            raise ValueError("fx_rate must be > 0")
        return f"{parsed}"


class CostingSettingsResponse(BaseModel):
    report_currency_code: str
    report_currency_symbol: str
    timezone_preference: str | None = None
    endpoint_fx_mappings: list[EndpointFxMapping]


class CostingSettingsUpdate(BaseModel):
    report_currency_code: str
    report_currency_symbol: str
    timezone_preference: str | None = None
    endpoint_fx_mappings: list[EndpointFxMapping] = []

    @field_validator("report_currency_code")
    @classmethod
    def validate_report_currency_code(cls, v: str) -> str:
        code = v.strip().upper()
        if not _CURRENCY_CODE_RE.match(code):
            raise ValueError(
                "report_currency_code must be a 3-letter uppercase ISO code"
            )
        return code

    @field_validator("report_currency_symbol")
    @classmethod
    def validate_report_currency_symbol(cls, v: str) -> str:
        symbol = v.strip()
        if not symbol:
            raise ValueError("report_currency_symbol must not be empty")
        if len(symbol) > 5:
            raise ValueError("report_currency_symbol must be at most 5 characters")
        return symbol

    @field_validator("timezone_preference")
    @classmethod
    def validate_timezone_preference(cls, v: str | None) -> str | None:
        if v is None:
            return None
        timezone = v.strip()
        if not timezone:
            return None
        if len(timezone) > 100:
            raise ValueError("timezone_preference must be at most 100 characters")
        return timezone

    @model_validator(mode="after")
    def validate_unique_mappings(self):
        seen: set[tuple[str, int]] = set()
        for mapping in self.endpoint_fx_mappings:
            key = (mapping.model_id, mapping.endpoint_id)
            if key in seen:
                raise ValueError(
                    f"Duplicate endpoint_fx_mapping for model_id={mapping.model_id}, endpoint_id={mapping.endpoint_id}"
                )
            seen.add(key)
        return self


class SpendingSummaryResponse(BaseModel):
    total_cost_micros: int
    successful_request_count: int
    priced_request_count: int
    unpriced_request_count: int
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_input_tokens: int
    total_cache_creation_input_tokens: int
    total_reasoning_tokens: int
    total_tokens: int
    avg_cost_per_successful_request_micros: int


class SpendingGroupRow(BaseModel):
    key: str
    total_cost_micros: int
    total_requests: int
    priced_requests: int
    unpriced_requests: int
    total_tokens: int


class SpendingTopModel(BaseModel):
    model_id: str
    total_cost_micros: int


class SpendingTopEndpoint(BaseModel):
    endpoint_id: int | None
    endpoint_label: str
    total_cost_micros: int


class SpendingReportResponse(BaseModel):
    summary: SpendingSummaryResponse
    groups: list[SpendingGroupRow]
    groups_total: int
    top_spending_models: list[SpendingTopModel]
    top_spending_endpoints: list[SpendingTopEndpoint]
    unpriced_breakdown: dict[str, int]
    report_currency_code: str
    report_currency_symbol: str


# --- Config Export/Import Schemas ---


class ConfigEndpointExport(BaseModel):
    endpoint_id: int | None = None
    name: str
    base_url: str
    api_key: str


class ConfigConnectionExport(BaseModel):
    connection_id: int | None = None
    endpoint_id: int
    is_active: bool = True
    priority: int = 0
    name: str | None = None
    description: str | None = None
    auth_type: str | None = None
    custom_headers: dict[str, str] | None = None
    pricing_enabled: bool = False
    pricing_currency_code: str | None = None
    input_price: str | None = None
    output_price: str | None = None
    cached_input_price: str | None = None
    cache_creation_price: str | None = None
    reasoning_price: str | None = None
    missing_special_token_price_policy: Literal["MAP_TO_OUTPUT", "ZERO_COST"] = (
        "MAP_TO_OUTPUT"
    )
    pricing_config_version: int = 0
    forward_stream_options: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_connection_name_fields(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data

        normalized = dict(data)
        if normalized.get("name") is None and normalized.get("description") is not None:
            normalized["name"] = normalized["description"]
        if (
            normalized.get("description") is None
            and normalized.get("name") is not None
        ):
            normalized["description"] = normalized["name"]
        return normalized

class ConfigModelExport(BaseModel):
    provider_type: str
    model_id: str
    display_name: str | None = None
    model_type: str = "native"
    redirect_to: str | None = None
    lb_strategy: Literal["single", "failover"] = "single"
    failover_recovery_enabled: bool = True
    failover_recovery_cooldown_seconds: int = 60
    is_enabled: bool = True
    connections: list[ConfigConnectionExport] = []


class ConfigProviderExport(BaseModel):
    name: str
    provider_type: str
    description: str | None = None
    audit_enabled: bool = False
    audit_capture_bodies: bool = True


class ConfigEndpointFxRateExport(BaseModel):
    model_id: str
    endpoint_id: int
    fx_rate: str


class ConfigUserSettingsExport(BaseModel):
    report_currency_code: str = "USD"
    report_currency_symbol: str = "$"
    endpoint_fx_mappings: list[ConfigEndpointFxRateExport] = []


class ConfigExportResponse(BaseModel):
    version: Literal[6] = 6
    exported_at: datetime
    providers: list[ConfigProviderExport]
    endpoints: list[ConfigEndpointExport]
    models: list[ConfigModelExport]
    user_settings: ConfigUserSettingsExport | None = None
    header_blocklist_rules: list["HeaderBlocklistRuleExport"] = []


class ConfigImportRequest(BaseModel):
    version: Literal[6]
    exported_at: datetime | None = None
    providers: list[ConfigProviderExport]
    endpoints: list[ConfigEndpointExport]
    models: list[ConfigModelExport]
    user_settings: ConfigUserSettingsExport | None = None
    header_blocklist_rules: list["HeaderBlocklistRuleExport"] | None = None


class ConfigImportResponse(BaseModel):
    providers_imported: int
    endpoints_imported: int
    models_imported: int
    connections_imported: int


# --- Audit Log Schemas ---


class AuditLogListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    request_log_id: int | None
    provider_id: int
    model_id: str
    endpoint_id: int | None = None
    connection_id: int | None = None
    endpoint_base_url: str | None = None
    endpoint_description: str | None = None
    request_method: str
    request_url: str
    request_headers: str
    request_body_preview: str | None
    response_status: int
    is_stream: bool
    duration_ms: int
    created_at: datetime


class AuditLogDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    request_log_id: int | None
    provider_id: int
    model_id: str
    endpoint_id: int | None = None
    connection_id: int | None = None
    endpoint_base_url: str | None = None
    endpoint_description: str | None = None
    request_method: str
    request_url: str
    request_headers: str
    request_body: str | None
    response_status: int
    response_headers: str | None
    response_body: str | None
    is_stream: bool
    duration_ms: int
    created_at: datetime


class AuditLogListResponse(BaseModel):
    items: list[AuditLogListItem]
    total: int
    limit: int
    offset: int


class AuditLogDeleteResponse(BaseModel):
    deleted_count: int


# --- Batch Delete Schemas ---


class BatchDeleteResponse(BaseModel):
    deleted_count: int


# --- Header Blocklist Rule Schemas ---


class HeaderBlocklistRuleCreate(BaseModel):
    name: str
    match_type: str
    pattern: str
    enabled: bool = True

    @field_validator("match_type")
    @classmethod
    def validate_match_type(cls, v: str) -> str:
        if v not in ("exact", "prefix"):
            raise ValueError("match_type must be 'exact' or 'prefix'")
        return v

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, v: str, info) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("pattern must not be empty")
        if not _HEADER_TOKEN_RE.match(v):
            raise ValueError(
                "pattern must contain only lowercase alphanumeric characters and hyphens, "
                "and must start with an alphanumeric character"
            )
        return v

    @field_validator("pattern")
    @classmethod
    def validate_prefix_ends_with_dash(cls, v: str, info) -> str:
        match_type = info.data.get("match_type")
        if match_type == "prefix" and not v.endswith("-"):
            raise ValueError("prefix pattern must end with '-'")
        return v


class HeaderBlocklistRuleUpdate(BaseModel):
    name: str | None = None
    match_type: str | None = None
    pattern: str | None = None
    enabled: bool | None = None

    @field_validator("match_type")
    @classmethod
    def validate_match_type(cls, v: str | None) -> str | None:
        if v is not None and v not in ("exact", "prefix"):
            raise ValueError("match_type must be 'exact' or 'prefix'")
        return v

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().lower()
        if not v:
            raise ValueError("pattern must not be empty")
        if not _HEADER_TOKEN_RE.match(v):
            raise ValueError(
                "pattern must contain only lowercase alphanumeric characters and hyphens, "
                "and must start with an alphanumeric character"
            )
        return v


class HeaderBlocklistRuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    match_type: str
    pattern: str
    enabled: bool
    is_system: bool
    created_at: datetime
    updated_at: datetime


class HeaderBlocklistRuleExport(BaseModel):
    name: str
    match_type: str
    pattern: str
    enabled: bool
    is_system: bool


class ConnectionDropdownItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    endpoint_id: int
    name: str | None
    description: str | None


class ConnectionDropdownResponse(BaseModel):
    items: list[ConnectionDropdownItem]
