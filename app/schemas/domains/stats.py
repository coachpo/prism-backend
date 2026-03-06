from datetime import datetime
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.schemas.domains.core import _CURRENCY_CODE_RE

# --- Statistics Schemas ---


class RequestLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
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
    profile_id: int | None = None
    report_currency_code: str
    report_currency_symbol: str
    timezone_preference: str | None = None
    endpoint_fx_mappings: list[EndpointFxMapping]


class CostingSettingsUpdate(BaseModel):
    profile_id: int | None = None
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
