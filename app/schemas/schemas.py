from datetime import datetime
from pydantic import BaseModel, ConfigDict, field_validator
import json


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


# --- Endpoint Schemas ---


class EndpointBase(BaseModel):
    base_url: str
    api_key: str
    is_active: bool = True
    priority: int = 0
    description: str | None = None
    auth_type: str | None = None
    custom_headers: dict[str, str] | None = None


class EndpointCreate(EndpointBase):
    pass


class EndpointUpdate(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    is_active: bool | None = None
    priority: int | None = None
    description: str | None = None
    auth_type: str | None = None
    custom_headers: dict[str, str] | None = None


class EndpointResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    model_config_id: int
    base_url: str
    api_key: str
    is_active: bool
    priority: int
    description: str | None
    auth_type: str | None
    custom_headers: dict[str, str] | None
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
    endpoint_id: int
    health_status: str
    checked_at: datetime
    detail: str
    response_time_ms: int


# --- Model Config Schemas ---


class ModelConfigBase(BaseModel):
    provider_id: int
    model_id: str
    display_name: str | None = None
    model_type: str = "native"
    redirect_to: str | None = None
    lb_strategy: str = "single"
    is_enabled: bool = True


class ModelConfigCreate(ModelConfigBase):
    pass


class ModelConfigUpdate(BaseModel):
    provider_id: int | None = None
    model_id: str | None = None
    display_name: str | None = None
    model_type: str | None = None
    redirect_to: str | None = None
    lb_strategy: str | None = None
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
    lb_strategy: str
    is_enabled: bool
    endpoints: list[EndpointResponse]
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
    lb_strategy: str
    is_enabled: bool
    endpoint_count: int
    active_endpoint_count: int
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
    endpoint_base_url: str | None
    status_code: int
    response_time_ms: int
    is_stream: bool
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    request_path: str
    error_detail: str | None
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


# --- Config Export/Import Schemas ---


class ConfigEndpointExport(BaseModel):
    base_url: str
    api_key: str
    is_active: bool = True
    priority: int = 0
    description: str | None = None
    auth_type: str | None = None
    custom_headers: dict[str, str] | None = None


class ConfigModelExport(BaseModel):
    provider_type: str
    model_id: str
    display_name: str | None = None
    model_type: str = "native"
    redirect_to: str | None = None
    lb_strategy: str = "single"
    is_enabled: bool = True
    endpoints: list[ConfigEndpointExport] = []


class ConfigProviderExport(BaseModel):
    name: str
    provider_type: str
    description: str | None = None
    audit_enabled: bool = False
    audit_capture_bodies: bool = True


class ConfigExportResponse(BaseModel):
    version: int = 1
    exported_at: datetime
    providers: list[ConfigProviderExport]
    models: list[ConfigModelExport]


class ConfigImportRequest(BaseModel):
    version: int
    exported_at: datetime | None = None
    providers: list[ConfigProviderExport]
    models: list[ConfigModelExport]


class ConfigImportResponse(BaseModel):
    providers_imported: int
    models_imported: int
    endpoints_imported: int


# --- Audit Log Schemas ---


class AuditLogListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    request_log_id: int | None
    provider_id: int
    model_id: str
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
