from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.domains.core import ApiFamily


class UsageRequestEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
    ingress_request_id: str
    model_id: str
    resolved_target_model_id: str | None = None
    api_family: ApiFamily
    endpoint_id: int | None = None
    connection_id: int | None = None
    proxy_api_key_id: int | None = None
    proxy_api_key_name_snapshot: str | None = None
    status_code: int
    success_flag: bool
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
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
    attempt_count: int
    request_path: str
    created_at: datetime


class UsageSnapshotTimeRange(BaseModel):
    preset: Literal["all", "7h", "24h", "7d"]
    start_at: datetime | None = None
    end_at: datetime


class UsageSnapshotCurrency(BaseModel):
    code: str
    symbol: str


class UsageSnapshotOverview(BaseModel):
    total_requests: int
    success_requests: int
    failed_requests: int
    success_rate: float
    total_tokens: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    average_rpm: float
    average_tpm: float
    total_cost_micros: int
    rolling_window_minutes: int
    rolling_request_count: int
    rolling_token_count: int
    rolling_rpm: float
    rolling_tpm: float


class UsageServiceHealthPoint(BaseModel):
    bucket_start: datetime
    request_count: int
    success_count: int
    failed_count: int
    availability_percentage: float | None = None


class UsageServiceHealthCell(BaseModel):
    bucket_start: datetime
    request_count: int
    success_count: int
    failed_count: int
    availability_percentage: float | None = None
    status: Literal["ok", "degraded", "down", "empty"]


class UsageServiceHealth(BaseModel):
    availability_percentage: float | None = None
    request_count: int
    success_count: int
    failed_count: int
    days: int
    interval_minutes: int
    daily: list[UsageServiceHealthPoint] = Field(default_factory=list)
    cells: list[UsageServiceHealthCell] = Field(default_factory=list)


class UsageRequestTrendPoint(BaseModel):
    bucket_start: datetime
    request_count: int
    success_count: int
    failed_count: int
    rpm: float


class UsageRequestTrendSeries(BaseModel):
    key: str
    label: str
    total_requests: int
    points: list[UsageRequestTrendPoint] = Field(default_factory=list)


class UsageRequestTrends(BaseModel):
    hourly: list[UsageRequestTrendSeries] = Field(default_factory=list)
    daily: list[UsageRequestTrendSeries] = Field(default_factory=list)


class UsageTokenTrendPoint(BaseModel):
    bucket_start: datetime
    total_tokens: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    tpm: float


class UsageTokenTrendSeries(BaseModel):
    key: str
    label: str
    total_tokens: int
    points: list[UsageTokenTrendPoint] = Field(default_factory=list)


class UsageTokenUsageTrends(BaseModel):
    hourly: list[UsageTokenTrendSeries] = Field(default_factory=list)
    daily: list[UsageTokenTrendSeries] = Field(default_factory=list)


class UsageTokenTypeBreakdownPoint(BaseModel):
    bucket_start: datetime
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int


class UsageTokenTypeBreakdown(BaseModel):
    hourly: list[UsageTokenTypeBreakdownPoint] = Field(default_factory=list)
    daily: list[UsageTokenTypeBreakdownPoint] = Field(default_factory=list)


class UsageCostOverviewPoint(BaseModel):
    bucket_start: datetime
    total_cost_micros: int


class UsageCostOverview(BaseModel):
    total_cost_micros: int
    priced_request_count: int
    unpriced_request_count: int
    hourly: list[UsageCostOverviewPoint] = Field(default_factory=list)
    daily: list[UsageCostOverviewPoint] = Field(default_factory=list)


class UsageEndpointModelStatistic(BaseModel):
    model_id: str
    model_label: str
    request_count: int
    success_rate: float
    total_tokens: int
    total_cost_micros: int


class UsageEndpointStatistic(BaseModel):
    endpoint_id: int | None = None
    endpoint_label: str
    request_count: int
    success_rate: float
    total_tokens: int
    total_cost_micros: int
    models: list[UsageEndpointModelStatistic] = Field(default_factory=list)


class UsageModelStatistic(BaseModel):
    model_id: str
    model_label: str
    request_count: int
    success_rate: float
    total_tokens: int
    total_cost_micros: int


class UsageProxyApiKeyStatistic(BaseModel):
    proxy_api_key_id: int | None = None
    proxy_api_key_label: str
    request_count: int
    success_rate: float
    total_tokens: int
    total_cost_micros: int


class UsageSnapshotResponse(BaseModel):
    generated_at: datetime
    time_range: UsageSnapshotTimeRange
    currency: UsageSnapshotCurrency
    overview: UsageSnapshotOverview
    service_health: UsageServiceHealth
    request_trends: UsageRequestTrends
    token_usage_trends: UsageTokenUsageTrends
    token_type_breakdown: UsageTokenTypeBreakdown
    cost_overview: UsageCostOverview
    endpoint_statistics: list[UsageEndpointStatistic] = Field(default_factory=list)
    model_statistics: list[UsageModelStatistic] = Field(default_factory=list)
    proxy_api_key_statistics: list[UsageProxyApiKeyStatistic] = Field(
        default_factory=list
    )
