from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.loadbalance_event_summary import describe_loadbalance_event
from app.schemas.domains.core import ApiFamily, _CURRENCY_CODE_RE

# --- Statistics Schemas ---


class RequestLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
    model_id: str
    api_family: ApiFamily
    vendor_id: int | None = None
    vendor_key: str | None = None
    vendor_name: str | None = None
    resolved_target_model_id: str | None = None
    endpoint_id: int | None
    connection_id: int | None
    proxy_api_key_id: int | None = None
    proxy_api_key_name_snapshot: str | None = None
    ingress_request_id: str | None = None
    attempt_number: int | None = None
    provider_correlation_id: str | None = None
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


class RequestLogListItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    model_id: str
    resolved_target_model_id: str | None = None
    api_family: ApiFamily
    vendor_id: int | None = None
    vendor_key: str | None = None
    vendor_name: str | None = None
    endpoint_id: int | None
    connection_id: int | None
    status_code: int
    response_time_ms: int
    is_stream: bool
    total_tokens: int | None = None
    total_cost_user_currency_micros: int | None = None
    report_currency_symbol: str | None = None


class RequestLogListResponse(BaseModel):
    items: list[RequestLogListItemResponse]
    total: int
    limit: int
    offset: int


class RequestLogDetailSummaryResponse(BaseModel):
    id: int
    created_at: datetime
    model_id: str
    resolved_target_model_id: str | None = None
    api_family: ApiFamily
    vendor_id: int | None = None
    vendor_key: str | None = None
    vendor_name: str | None = None
    status_code: int
    response_time_ms: int
    is_stream: bool


class RequestLogDetailRequestResponse(BaseModel):
    request_path: str
    ingress_request_id: str | None = None
    attempt_number: int | None = None
    provider_correlation_id: str | None = None
    proxy_api_key_id: int | None = None
    proxy_api_key_name_snapshot: str | None = None
    error_detail: str | None = None


class RequestLogDetailRoutingResponse(BaseModel):
    profile_id: int
    model_id: str
    resolved_target_model_id: str | None = None
    api_family: ApiFamily
    vendor_id: int | None = None
    vendor_key: str | None = None
    vendor_name: str | None = None
    endpoint_id: int | None
    connection_id: int | None
    endpoint_base_url: str | None
    endpoint_description: str | None = None


class RequestLogDetailUsageResponse(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    success_flag: bool | None = None
    billable_flag: bool | None = None
    priced_flag: bool | None = None
    unpriced_reason: str | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_tokens: int | None = None


class RequestLogDetailCostingResponse(BaseModel):
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


class RequestLogDetailPricingResponse(BaseModel):
    pricing_snapshot_unit: str | None = None
    pricing_snapshot_input: str | None = None
    pricing_snapshot_output: str | None = None
    pricing_snapshot_cache_read_input: str | None = None
    pricing_snapshot_cache_creation_input: str | None = None
    pricing_snapshot_reasoning: str | None = None
    pricing_snapshot_missing_special_token_price_policy: str | None = None
    pricing_config_version_used: int | None = None


class RequestLogDetailResponse(BaseModel):
    summary: RequestLogDetailSummaryResponse
    request: RequestLogDetailRequestResponse
    routing: RequestLogDetailRoutingResponse
    usage: RequestLogDetailUsageResponse
    costing: RequestLogDetailCostingResponse
    pricing: RequestLogDetailPricingResponse

    @classmethod
    def from_request_log(cls, entry: Any) -> "RequestLogDetailResponse":
        return cls(
            summary=RequestLogDetailSummaryResponse(
                id=entry.id,
                created_at=entry.created_at,
                model_id=entry.model_id,
                resolved_target_model_id=entry.resolved_target_model_id,
                api_family=entry.api_family,
                vendor_id=entry.vendor_id,
                vendor_key=entry.vendor_key,
                vendor_name=entry.vendor_name,
                status_code=entry.status_code,
                response_time_ms=entry.response_time_ms,
                is_stream=entry.is_stream,
            ),
            request=RequestLogDetailRequestResponse(
                request_path=entry.request_path,
                ingress_request_id=entry.ingress_request_id,
                attempt_number=entry.attempt_number,
                provider_correlation_id=entry.provider_correlation_id,
                proxy_api_key_id=entry.proxy_api_key_id,
                proxy_api_key_name_snapshot=entry.proxy_api_key_name_snapshot,
                error_detail=entry.error_detail,
            ),
            routing=RequestLogDetailRoutingResponse(
                profile_id=entry.profile_id,
                model_id=entry.model_id,
                resolved_target_model_id=entry.resolved_target_model_id,
                api_family=entry.api_family,
                vendor_id=entry.vendor_id,
                vendor_key=entry.vendor_key,
                vendor_name=entry.vendor_name,
                endpoint_id=entry.endpoint_id,
                connection_id=entry.connection_id,
                endpoint_base_url=entry.endpoint_base_url,
                endpoint_description=entry.endpoint_description,
            ),
            usage=RequestLogDetailUsageResponse(
                input_tokens=entry.input_tokens,
                output_tokens=entry.output_tokens,
                total_tokens=entry.total_tokens,
                success_flag=entry.success_flag,
                billable_flag=entry.billable_flag,
                priced_flag=entry.priced_flag,
                unpriced_reason=entry.unpriced_reason,
                cache_read_input_tokens=entry.cache_read_input_tokens,
                cache_creation_input_tokens=entry.cache_creation_input_tokens,
                reasoning_tokens=entry.reasoning_tokens,
            ),
            costing=RequestLogDetailCostingResponse(
                input_cost_micros=entry.input_cost_micros,
                output_cost_micros=entry.output_cost_micros,
                cache_read_input_cost_micros=entry.cache_read_input_cost_micros,
                cache_creation_input_cost_micros=entry.cache_creation_input_cost_micros,
                reasoning_cost_micros=entry.reasoning_cost_micros,
                total_cost_original_micros=entry.total_cost_original_micros,
                total_cost_user_currency_micros=entry.total_cost_user_currency_micros,
                currency_code_original=entry.currency_code_original,
                report_currency_code=entry.report_currency_code,
                report_currency_symbol=entry.report_currency_symbol,
                fx_rate_used=entry.fx_rate_used,
                fx_rate_source=entry.fx_rate_source,
            ),
            pricing=RequestLogDetailPricingResponse(
                pricing_snapshot_unit=entry.pricing_snapshot_unit,
                pricing_snapshot_input=entry.pricing_snapshot_input,
                pricing_snapshot_output=entry.pricing_snapshot_output,
                pricing_snapshot_cache_read_input=entry.pricing_snapshot_cache_read_input,
                pricing_snapshot_cache_creation_input=entry.pricing_snapshot_cache_creation_input,
                pricing_snapshot_reasoning=entry.pricing_snapshot_reasoning,
                pricing_snapshot_missing_special_token_price_policy=entry.pricing_snapshot_missing_special_token_price_policy,
                pricing_config_version_used=entry.pricing_config_version_used,
            ),
        )


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


class ModelMetricsBatchRequest(BaseModel):
    model_ids: list[str]
    summary_window_hours: int = 24
    spending_preset: Literal[
        "today", "last_7_days", "last_30_days", "custom", "all"
    ] = "last_30_days"

    @field_validator("model_ids")
    @classmethod
    def validate_model_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if not normalized:
            raise ValueError("model_ids must contain at least one model id")
        return list(dict.fromkeys(normalized))

    @field_validator("summary_window_hours")
    @classmethod
    def validate_summary_window_hours(cls, value: int) -> int:
        if value < 1 or value > 24 * 30:
            raise ValueError("summary_window_hours must be between 1 and 720")
        return value


class ModelMetricsBatchItem(BaseModel):
    model_id: str
    success_rate: float
    request_count_24h: int
    p95_latency_ms: int
    spend_30d_micros: int


class ModelMetricsBatchResponse(BaseModel):
    items: list[ModelMetricsBatchItem]


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


class TimezonePreferenceResponse(BaseModel):
    profile_id: int | None = None
    timezone_preference: str | None = None


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


class TimezonePreferenceUpdate(BaseModel):
    timezone_preference: str | None = None

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


# --- Loadbalance Event Schemas ---


class LoadbalanceEventSummary(BaseModel):
    event: str
    reason: str
    operation: str
    cooldown: str


class LoadbalanceEventListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
    connection_id: int
    event_type: str
    failure_kind: str | None
    consecutive_failures: int
    cooldown_seconds: float
    blocked_until_mono: float | None
    model_id: str | None
    endpoint_id: int | None
    vendor_id: int | None
    max_cooldown_strikes: int | None = None
    ban_mode: str | None = None
    banned_until_at: datetime | None = None
    summary: LoadbalanceEventSummary
    created_at: datetime

    @model_validator(mode="before")
    @classmethod
    def populate_summary(cls, data: Any) -> Any:
        if isinstance(data, dict):
            event_type = data.get("event_type")
            if not event_type:
                return data
            return {
                **data,
                "summary": describe_loadbalance_event(
                    event_type=event_type,
                    failure_kind=data.get("failure_kind"),
                    consecutive_failures=data.get("consecutive_failures", 0),
                    cooldown_seconds=float(data.get("cooldown_seconds", 0.0)),
                    failure_threshold=data.get("failure_threshold"),
                ),
            }

        if hasattr(data, "event_type"):
            payload = {
                field_name: getattr(data, field_name)
                for field_name in cls.model_fields
                if field_name != "summary" and hasattr(data, field_name)
            }
            payload["summary"] = describe_loadbalance_event(
                event_type=getattr(data, "event_type"),
                failure_kind=getattr(data, "failure_kind", None),
                consecutive_failures=getattr(data, "consecutive_failures", 0),
                cooldown_seconds=float(getattr(data, "cooldown_seconds", 0.0)),
                failure_threshold=getattr(data, "failure_threshold", None),
            )
            return payload

        return data


class LoadbalanceEventDetail(LoadbalanceEventListItem):
    failure_threshold: int | None
    backoff_multiplier: float | None
    max_cooldown_seconds: int | None


class LoadbalanceEventListResponse(BaseModel):
    items: list[LoadbalanceEventListItem]
    total: int
    limit: int
    offset: int


class LoadbalanceEventDeleteResponse(BaseModel):
    accepted: bool


LoadbalanceCurrentStateValue = Literal[
    "counting", "blocked", "probe_eligible", "banned"
]


class LoadbalanceCurrentStateItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    connection_id: int
    circuit_state: str | None = None
    probe_available_at: datetime | None = None
    window_started_at: datetime | None = None
    window_request_count: int = 0
    in_flight_non_stream: int = 0
    in_flight_stream: int = 0
    consecutive_failures: int
    last_failure_kind: str | None
    last_cooldown_seconds: float
    max_cooldown_strikes: int
    ban_mode: str
    banned_until_at: datetime | None
    blocked_until_at: datetime | None
    probe_eligible_logged: bool
    live_p95_latency_ms: int | None = None
    last_live_failure_at: datetime | None = None
    last_live_success_at: datetime | None = None
    state: LoadbalanceCurrentStateValue
    created_at: datetime
    updated_at: datetime


class LoadbalanceCurrentStateListResponse(BaseModel):
    items: list[LoadbalanceCurrentStateItem]


class LoadbalanceCurrentStateResetResponse(BaseModel):
    connection_id: int
    cleared: bool


# --- Throughput Schemas ---


class ThroughputBucket(BaseModel):
    timestamp: datetime
    request_count: int
    rpm: float


class ThroughputStatsResponse(BaseModel):
    average_rpm: float
    peak_rpm: float
    current_rpm: float
    total_requests: int
    time_window_seconds: float
    buckets: list[ThroughputBucket]


class DashboardRouteSnapshotResponse(BaseModel):
    model_id: str
    model_config_id: int | None = None
    model_label: str
    endpoint_id: int
    endpoint_label: str
    active_connection_count: int
    traffic_request_count_24h: int
    request_count_24h: int
    success_count_24h: int
    error_count_24h: int
    success_rate_24h: float | None = None


class DashboardRealtimeUpdateResponse(BaseModel):
    request_log: RequestLogResponse
    stats_summary_24h: StatsSummaryResponse
    api_family_summary_24h: StatsSummaryResponse
    spending_summary_30d: SpendingReportResponse
    throughput_24h: ThroughputStatsResponse
    routing_route_24h: DashboardRouteSnapshotResponse | None = None
