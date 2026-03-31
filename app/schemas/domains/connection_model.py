import json
from datetime import datetime
from typing import Annotated
from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.services.loadbalancer.policy import (
    normalize_failover_status_codes,
    resolve_effective_loadbalance_policy,
    serialize_routing_policy,
)

from .common import ApiFamily, AuthType
from .endpoint_pricing import (
    ConnectionPricingTemplateSummary,
    EndpointCreate,
    EndpointResponse,
)
from .profile_vendor import VendorResponse


class ConnectionBase(BaseModel):
    is_active: bool = True
    name: str | None = None
    auth_type: AuthType | None = None
    custom_headers: dict[str, str] | None = None
    pricing_template_id: int | None = None
    qps_limit: int | None = Field(default=None, ge=1)
    max_in_flight_non_stream: int | None = Field(default=None, ge=1)
    max_in_flight_stream: int | None = Field(default=None, ge=1)
    openai_probe_endpoint_variant: Literal["responses", "chat_completions"] = (
        "responses"
    )


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
    qps_limit: int | None = Field(default=None, ge=1)
    max_in_flight_non_stream: int | None = Field(default=None, ge=1)
    max_in_flight_stream: int | None = Field(default=None, ge=1)
    openai_probe_endpoint_variant: Literal["responses", "chat_completions"] | None = (
        None
    )

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
    qps_limit: int | None = None
    max_in_flight_non_stream: int | None = None
    max_in_flight_stream: int | None = None
    openai_probe_endpoint_variant: Literal["responses", "chat_completions"] = (
        "responses"
    )
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
    def parse_custom_headers(
        cls, v: str | dict[str, str] | None
    ) -> dict[str, str] | None:
        if v is None:
            return None
        if isinstance(v, dict):
            return v
        return json.loads(v)

    @field_validator("openai_probe_endpoint_variant", mode="before")
    @classmethod
    def normalize_openai_probe_endpoint_variant(
        cls, value: str | None
    ) -> Literal["responses", "chat_completions"]:
        if value == "chat_completions":
            return "chat_completions"
        return "responses"


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


class ModelConnectionsBatchRequest(BaseModel):
    model_config_ids: list[int]

    @field_validator("model_config_ids")
    @classmethod
    def validate_model_config_ids(cls, value: list[int]) -> list[int]:
        normalized = list(dict.fromkeys(value))
        if not normalized:
            raise ValueError(
                "model_config_ids must contain at least one model config id"
            )
        return normalized


class ModelConnectionsBatchItem(BaseModel):
    model_config_id: int
    connections: list[ConnectionResponse]


class ModelConnectionsBatchResponse(BaseModel):
    items: list[ModelConnectionsBatchItem]


class RoutingPolicyHedge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    delay_ms: int = Field(default=1500, ge=0, le=300_000)
    max_additional_attempts: int = Field(default=1, ge=1, le=10)


class RoutingPolicyCircuitBreaker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_status_codes: list[int] = Field(
        default_factory=lambda: [403, 422, 429, 500, 502, 503, 504, 529]
    )
    base_open_seconds: int = Field(default=60, ge=0, le=86_400)
    failure_threshold: int = Field(default=2, ge=1, le=50)
    backoff_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    max_open_seconds: int = Field(default=900, ge=1, le=86_400)
    jitter_ratio: float = Field(default=0.2, ge=0.0, le=1.0)
    ban_mode: Literal["off", "manual", "temporary"] = "off"
    max_open_strikes_before_ban: int = Field(default=0, ge=0, le=100)
    ban_duration_seconds: int = Field(default=0, ge=0, le=86_400)

    @field_validator("failure_status_codes", mode="before")
    @classmethod
    def validate_status_codes(cls, value: object) -> list[int]:
        return list(normalize_failover_status_codes(value))

    @model_validator(mode="after")
    def validate_ban_policy(self):
        if self.ban_mode == "off":
            if self.max_open_strikes_before_ban != 0:
                raise ValueError(
                    "ban_mode='off' requires max_open_strikes_before_ban=0"
                )
            if self.ban_duration_seconds != 0:
                raise ValueError("ban_mode='off' requires ban_duration_seconds=0")
        if self.ban_mode == "manual":
            if self.max_open_strikes_before_ban < 1:
                raise ValueError(
                    "ban_mode='manual' requires max_open_strikes_before_ban >= 1"
                )
            if self.ban_duration_seconds != 0:
                raise ValueError("ban_mode='manual' requires ban_duration_seconds=0")
        if self.ban_mode == "temporary":
            if self.max_open_strikes_before_ban < 1:
                raise ValueError(
                    "ban_mode='temporary' requires max_open_strikes_before_ban >= 1"
                )
            if self.ban_duration_seconds < 1:
                raise ValueError(
                    "ban_mode='temporary' requires ban_duration_seconds >= 1"
                )
        return self


class RoutingPolicyAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    respect_qps_limit: bool = True
    respect_in_flight_limits: bool = True


class RoutingPolicyMonitoring(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    stale_after_seconds: int = Field(default=300, ge=1, le=86_400)
    endpoint_ping_weight: float = Field(default=1.0, ge=0.0, le=10.0)
    conversation_delay_weight: float = Field(default=1.0, ge=0.0, le=10.0)
    failure_penalty_weight: float = Field(default=2.0, ge=0.0, le=10.0)


class RoutingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["adaptive"] = "adaptive"
    routing_objective: Literal["minimize_latency", "maximize_availability"] = (
        "minimize_latency"
    )
    deadline_budget_ms: int = Field(default=30_000, ge=1, le=300_000)
    hedge: RoutingPolicyHedge = Field(default_factory=RoutingPolicyHedge)
    circuit_breaker: RoutingPolicyCircuitBreaker = Field(
        default_factory=RoutingPolicyCircuitBreaker
    )
    admission: RoutingPolicyAdmission = Field(default_factory=RoutingPolicyAdmission)
    monitoring: RoutingPolicyMonitoring = Field(default_factory=RoutingPolicyMonitoring)


class LoadbalanceStrategyBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    routing_policy: RoutingPolicy

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be empty")
        return normalized


class LoadbalanceStrategyCreate(LoadbalanceStrategyBase):
    pass


class LoadbalanceStrategyUpdate(LoadbalanceStrategyBase):
    pass


class LoadbalanceStrategySummary(LoadbalanceStrategyBase):
    model_config = ConfigDict(from_attributes=True)

    id: int

    @model_validator(mode="before")
    @classmethod
    def resolve_effective_policy_from_orm(cls, value: object) -> object:
        if value is None or isinstance(value, (cls, dict)):
            return value
        if not hasattr(value, "id") or not hasattr(value, "name"):
            return value

        policy = resolve_effective_loadbalance_policy(value)
        return {
            "id": getattr(value, "id"),
            "name": getattr(value, "name"),
            "routing_policy": serialize_routing_policy(policy),
        }


class LoadbalanceStrategyResponse(LoadbalanceStrategySummary):
    profile_id: int
    attached_model_count: int
    created_at: datetime
    updated_at: datetime


class ProxyTargetReference(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    target_model_id: str
    position: int = Field(ge=0)

    @field_validator("target_model_id")
    @classmethod
    def validate_target_model_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("target_model_id must not be empty")
        return normalized


def validate_unique_proxy_targets(
    proxy_targets: list[ProxyTargetReference] | None,
) -> None:
    if not proxy_targets:
        return

    target_model_ids = [proxy_target.target_model_id for proxy_target in proxy_targets]
    if len(target_model_ids) != len(set(target_model_ids)):
        raise ValueError("proxy_targets must contain unique target_model_id values")


class ModelConfigBase(BaseModel):
    vendor_id: int
    api_family: ApiFamily
    model_id: str
    display_name: str | None = None
    model_type: Literal["native", "proxy"] = "native"
    proxy_targets: list[ProxyTargetReference] = Field(default_factory=list)
    loadbalance_strategy_id: int | None = None

    @model_validator(mode="after")
    def validate_strategy_attachment(self):
        if self.model_type == "native" and self.loadbalance_strategy_id is None:
            raise ValueError("loadbalance_strategy_id is required for native models")
        if self.model_type == "native" and self.proxy_targets:
            raise ValueError("proxy_targets must be empty for native models")
        if self.model_type == "proxy" and self.loadbalance_strategy_id is not None:
            raise ValueError("loadbalance_strategy_id must be null for proxy models")
        validate_unique_proxy_targets(self.proxy_targets)
        return self

    is_enabled: bool = True


class ModelConfigCreate(ModelConfigBase):
    pass


class ModelConfigUpdate(BaseModel):
    vendor_id: int | None = None
    api_family: ApiFamily | None = None
    model_id: str | None = None
    display_name: str | None = None
    model_type: Literal["native", "proxy"] | None = None
    proxy_targets: list[ProxyTargetReference] | None = None
    loadbalance_strategy_id: int | None = None
    is_enabled: bool | None = None

    @model_validator(mode="after")
    def validate_proxy_targets(self):
        if self.model_type == "native" and self.proxy_targets:
            raise ValueError("proxy_targets must be empty for native models")
        validate_unique_proxy_targets(self.proxy_targets)
        return self


class ModelConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
    vendor_id: int
    vendor: VendorResponse
    api_family: ApiFamily
    model_id: str
    display_name: str | None
    model_type: Literal["native", "proxy"]
    proxy_targets: list[ProxyTargetReference] = Field(default_factory=list)
    loadbalance_strategy_id: int | None
    loadbalance_strategy: LoadbalanceStrategySummary | None = None
    is_enabled: bool
    connections: list[ConnectionResponse]
    created_at: datetime
    updated_at: datetime


class ModelConfigListResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
    vendor_id: int
    vendor: VendorResponse
    api_family: ApiFamily
    model_id: str
    display_name: str | None
    model_type: Literal["native", "proxy"]
    proxy_targets: list[ProxyTargetReference] = Field(default_factory=list)
    loadbalance_strategy_id: int | None
    loadbalance_strategy: LoadbalanceStrategySummary | None = None
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
    "LoadbalanceStrategyCreate",
    "LoadbalanceStrategyResponse",
    "LoadbalanceStrategySummary",
    "LoadbalanceStrategyUpdate",
    "ModelConnectionsBatchItem",
    "ModelConnectionsBatchRequest",
    "ModelConnectionsBatchResponse",
    "ModelConfigBase",
    "ModelConfigCreate",
    "ModelConfigListResponse",
    "ModelConfigResponse",
    "ModelConfigUpdate",
    "ProxyTargetReference",
]
