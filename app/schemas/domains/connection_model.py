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
    serialize_auto_recovery,
)
from app.services.proxy_support.constants import DEFAULT_FAILOVER_STATUS_CODES

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


class AutoRecoveryCooldown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_seconds: int = Field(ge=0)
    failure_threshold: int = Field(ge=1, le=10)
    backoff_multiplier: float = Field(ge=1.0, le=10.0)
    max_cooldown_seconds: int = Field(ge=1, le=86_400)
    jitter_ratio: float = Field(ge=0.0, le=1.0)


class AutoRecoveryBanOff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["off"] = "off"


class AutoRecoveryBanManual(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["manual"] = "manual"
    max_cooldown_strikes_before_ban: int = Field(ge=1, le=100)


class AutoRecoveryBanTemporary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["temporary"] = "temporary"
    max_cooldown_strikes_before_ban: int = Field(ge=1, le=100)
    ban_duration_seconds: int = Field(ge=1, le=86_400)


AutoRecoveryBan = Annotated[
    AutoRecoveryBanOff | AutoRecoveryBanManual | AutoRecoveryBanTemporary,
    Field(discriminator="mode"),
]


class AutoRecoveryDisabled(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["disabled"] = "disabled"


class AutoRecoveryEnabled(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["enabled"] = "enabled"
    status_codes: list[int] = Field(
        default_factory=lambda: list(DEFAULT_FAILOVER_STATUS_CODES)
    )
    cooldown: AutoRecoveryCooldown
    ban: AutoRecoveryBan

    @field_validator("status_codes", mode="before")
    @classmethod
    def validate_status_codes(cls, value: object) -> list[int]:
        return list(normalize_failover_status_codes(value))


AutoRecovery = Annotated[
    AutoRecoveryDisabled | AutoRecoveryEnabled,
    Field(discriminator="mode"),
]


class LoadbalanceStrategyBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    strategy_type: Literal["single", "fill-first", "round-robin", "failover"]
    auto_recovery: AutoRecovery

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be empty")
        return normalized

    @model_validator(mode="after")
    def validate_strategy_behavior(self):
        if self.strategy_type == "single" and self.auto_recovery.mode != "disabled":
            raise ValueError("single strategies must not enable failover recovery")
        return self


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
            "strategy_type": policy.strategy_type,
            "auto_recovery": serialize_auto_recovery(policy),
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
