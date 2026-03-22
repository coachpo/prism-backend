from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.domains.core import AuthType, _HEADER_TOKEN_RE

# --- Config Export/Import Schemas ---


class ConfigEndpointExport(BaseModel):
    name: str
    base_url: str
    api_key: str
    position: int | None = Field(default=None, ge=0)


class ConfigEndpointImport(ConfigEndpointExport):
    endpoint_id: int | None = None


class ConfigPricingTemplateExport(BaseModel):
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
    version: int = 1


class ConfigPricingTemplateImport(ConfigPricingTemplateExport):
    pricing_template_id: int | None = None


class ConfigConnectionExport(BaseModel):
    endpoint_name: str
    pricing_template_name: str | None = None
    is_active: bool = True
    priority: int = Field(default=0, ge=0)
    name: str | None = None
    auth_type: AuthType | None = None
    custom_headers: dict[str, str] | None = None


class ConfigConnectionImport(BaseModel):
    connection_id: int | None = None
    endpoint_id: int | None = None
    endpoint_name: str | None = None
    pricing_template_id: int | None = None
    pricing_template_name: str | None = None
    is_active: bool = True
    priority: int = Field(default=0, ge=0)
    name: str | None = None
    auth_type: AuthType | None = None
    custom_headers: dict[str, str] | None = None


class ConfigModelExport(BaseModel):
    provider_type: str
    model_id: str
    display_name: str | None = None
    model_type: Literal["native", "proxy"] = "native"
    redirect_to: str | None = None
    lb_strategy: Literal["single", "failover"] = "single"
    failover_recovery_enabled: bool = True
    failover_recovery_cooldown_seconds: int = 60
    is_enabled: bool = True
    connections: list[ConfigConnectionExport] = Field(default_factory=list)


class ConfigModelImport(BaseModel):
    provider_type: str
    model_id: str
    display_name: str | None = None
    model_type: Literal["native", "proxy"] = "native"
    redirect_to: str | None = None
    lb_strategy: Literal["single", "failover"] = "single"
    failover_recovery_enabled: bool = True
    failover_recovery_cooldown_seconds: int = 60
    is_enabled: bool = True
    connections: list[ConfigConnectionImport] = Field(default_factory=list)


class ConfigEndpointFxRateExport(BaseModel):
    model_id: str
    endpoint_name: str
    fx_rate: str


class ConfigEndpointFxRateImport(BaseModel):
    model_id: str
    endpoint_id: int | None = None
    endpoint_name: str | None = None
    fx_rate: str


class ConfigUserSettingsExport(BaseModel):
    report_currency_code: str = "USD"
    report_currency_symbol: str = "$"
    endpoint_fx_mappings: list[ConfigEndpointFxRateExport] = Field(default_factory=list)


class ConfigUserSettingsImport(BaseModel):
    report_currency_code: str = "USD"
    report_currency_symbol: str = "$"
    endpoint_fx_mappings: list[ConfigEndpointFxRateImport] = Field(default_factory=list)


class ConfigExportResponse(BaseModel):
    version: Literal[2] = 2
    exported_at: datetime
    endpoints: list[ConfigEndpointExport]
    pricing_templates: list[ConfigPricingTemplateExport]
    models: list[ConfigModelExport]
    user_settings: ConfigUserSettingsExport | None = None
    header_blocklist_rules: list["HeaderBlocklistRuleExport"] = Field(
        default_factory=list
    )


class ConfigImportRequest(BaseModel):
    version: int
    exported_at: datetime | None = None
    endpoints: list[ConfigEndpointImport]
    pricing_templates: list[ConfigPricingTemplateImport]
    models: list[ConfigModelImport]
    user_settings: ConfigUserSettingsImport | None = None
    header_blocklist_rules: list["HeaderBlocklistRuleExport"] = Field(
        default_factory=list
    )


class ConfigImportResponse(BaseModel):
    endpoints_imported: int
    pricing_templates_imported: int
    models_imported: int
    connections_imported: int


# --- Audit Log Schemas ---


class AuditLogListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    request_log_id: int | None
    profile_id: int
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
    profile_id: int
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
    accepted: bool


# --- Batch Delete Schemas ---


class BatchDeleteResponse(BaseModel):
    accepted: bool


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
    profile_id: int | None
    created_at: datetime
    updated_at: datetime


class HeaderBlocklistRuleExport(HeaderBlocklistRuleCreate):
    pass


class ConnectionDropdownItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    endpoint_id: int
    name: str | None


class ConnectionDropdownResponse(BaseModel):
    items: list[ConnectionDropdownItem]
