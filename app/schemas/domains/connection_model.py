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

from app.services.loadbalancer.policy import (
    BanMode,
    normalize_strategy_ban_policy,
    resolve_effective_loadbalance_policy,
    validate_strategy_ban_policy,
)

from .common import ApiFamily, AuthType
from .endpoint_pricing import (
    ConnectionPricingTemplateSummary,
    EndpointCreate,
    EndpointResponse,
)
from .profile_provider import VendorResponse


class ConnectionBase(BaseModel):
    is_active: bool = True
    name: str | None = None
    auth_type: AuthType | None = None
    custom_headers: dict[str, str] | None = None
    pricing_template_id: int | None = None
    qps_limit: int | None = Field(default=None, ge=1)
    max_in_flight_non_stream: int | None = Field(default=None, ge=1)
    max_in_flight_stream: int | None = Field(default=None, ge=1)


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


class LoadbalanceStrategyBase(BaseModel):
    name: str
    strategy_type: Literal["single", "fill-first", "failover"] = "single"
    failover_recovery_enabled: bool = False
    failover_cooldown_seconds: int = Field(default=60, ge=0)
    failover_failure_threshold: int = Field(default=2, ge=1, le=10)
    failover_backoff_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    failover_max_cooldown_seconds: int = Field(default=900, ge=1, le=86_400)
    failover_jitter_ratio: float = Field(default=0.2, ge=0.0, le=1.0)
    failover_auth_error_cooldown_seconds: int = Field(default=1800, ge=1, le=86_400)
    failover_ban_mode: BanMode = "off"
    failover_max_cooldown_strikes_before_ban: int = Field(default=0, ge=0, le=100)
    failover_ban_duration_seconds: int = Field(default=0, ge=0, le=86_400)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be empty")
        return normalized

    @model_validator(mode="after")
    def validate_strategy_behavior(self):
        if self.strategy_type == "single" and self.failover_recovery_enabled:
            raise ValueError("single strategies must not enable failover recovery")
        validate_strategy_ban_policy(
            strategy_type=self.strategy_type,
            failover_recovery_enabled=self.failover_recovery_enabled,
            failover_ban_mode=self.failover_ban_mode,
            failover_max_cooldown_strikes_before_ban=self.failover_max_cooldown_strikes_before_ban,
            failover_ban_duration_seconds=self.failover_ban_duration_seconds,
        )
        return self


class LoadbalanceStrategyCreate(LoadbalanceStrategyBase):
    pass


class LoadbalanceStrategyUpdate(BaseModel):
    name: str | None = None
    strategy_type: Literal["single", "fill-first", "failover"] | None = None
    failover_recovery_enabled: bool | None = None
    failover_cooldown_seconds: int | None = Field(default=None, ge=0)
    failover_failure_threshold: int | None = Field(default=None, ge=1, le=10)
    failover_backoff_multiplier: float | None = Field(default=None, ge=1.0, le=10.0)
    failover_max_cooldown_seconds: int | None = Field(default=None, ge=1, le=86_400)
    failover_jitter_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    failover_auth_error_cooldown_seconds: int | None = Field(
        default=None, ge=1, le=86_400
    )
    failover_ban_mode: BanMode | None = None
    failover_max_cooldown_strikes_before_ban: int | None = Field(
        default=None, ge=0, le=100
    )
    failover_ban_duration_seconds: int | None = Field(default=None, ge=0, le=86_400)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be empty")
        return normalized

    @model_validator(mode="after")
    def validate_strategy_behavior(self):
        if self.strategy_type == "single" and self.failover_recovery_enabled:
            raise ValueError("single strategies must not enable failover recovery")
        return self


class LoadbalanceStrategySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    strategy_type: Literal["single", "fill-first", "failover"]
    failover_recovery_enabled: bool
    failover_cooldown_seconds: int
    failover_failure_threshold: int
    failover_backoff_multiplier: float
    failover_max_cooldown_seconds: int
    failover_jitter_ratio: float
    failover_auth_error_cooldown_seconds: int
    failover_ban_mode: BanMode
    failover_max_cooldown_strikes_before_ban: int
    failover_ban_duration_seconds: int

    @model_validator(mode="before")
    @classmethod
    def resolve_effective_policy_from_orm(cls, value: object) -> object:
        if value is None or isinstance(value, (cls, dict)):
            return value
        if not hasattr(value, "id") or not hasattr(value, "name"):
            return value

        policy = resolve_effective_loadbalance_policy(value)
        ban_mode, ban_strikes, ban_duration = normalize_strategy_ban_policy(
            strategy_type=policy.strategy_type,
            failover_recovery_enabled=policy.failover_recovery_enabled,
            failover_ban_mode=policy.failover_ban_mode,
            failover_max_cooldown_strikes_before_ban=policy.failover_max_cooldown_strikes_before_ban,
            failover_ban_duration_seconds=policy.failover_ban_duration_seconds,
        )
        return {
            "id": getattr(value, "id"),
            "name": getattr(value, "name"),
            "strategy_type": policy.strategy_type,
            "failover_recovery_enabled": policy.failover_recovery_enabled,
            "failover_cooldown_seconds": int(policy.failover_cooldown_seconds),
            "failover_failure_threshold": policy.failover_failure_threshold,
            "failover_backoff_multiplier": policy.failover_backoff_multiplier,
            "failover_max_cooldown_seconds": policy.failover_max_cooldown_seconds,
            "failover_jitter_ratio": policy.failover_jitter_ratio,
            "failover_auth_error_cooldown_seconds": (
                policy.failover_auth_error_cooldown_seconds
            ),
            "failover_ban_mode": ban_mode,
            "failover_max_cooldown_strikes_before_ban": ban_strikes,
            "failover_ban_duration_seconds": ban_duration,
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
        if self.model_type == "proxy" and not self.proxy_targets:
            raise ValueError("proxy_targets is required for proxy models")
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
        if self.model_type == "proxy" and self.proxy_targets == []:
            raise ValueError("proxy_targets must not be empty for proxy models")
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
