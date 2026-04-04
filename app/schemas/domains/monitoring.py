from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class MonitoringConnectionHistoryItem(BaseModel):
    checked_at: datetime
    endpoint_ping_status: str
    endpoint_ping_ms: Optional[int] = None
    conversation_status: str
    conversation_delay_ms: Optional[int] = None
    failure_kind: Optional[str] = None


class MonitoringConnectionBaseRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    connection_id: int
    connection_name: Optional[str] = None
    endpoint_id: int
    endpoint_name: str
    last_probe_status: Optional[str] = None
    circuit_state: Optional[str] = None
    live_p95_latency_ms: Optional[int] = None
    last_live_failure_kind: Optional[str] = None
    last_live_failure_at: Optional[datetime] = None
    last_live_success_at: Optional[datetime] = None
    endpoint_ping_status: str
    endpoint_ping_ms: Optional[int] = None
    conversation_status: str
    conversation_delay_ms: Optional[int] = None
    fused_status: str
    recent_history: list[MonitoringConnectionHistoryItem] = Field(default_factory=list)


class MonitoringModelConnectionRow(MonitoringConnectionBaseRow):
    pass


class MonitoringOverviewVendorItem(BaseModel):
    vendor_id: int
    vendor_key: str
    vendor_name: str
    icon_key: Optional[str] = None
    fused_status: str = "unknown"
    model_count: int
    connection_count: int
    healthy_connection_count: int
    degraded_connection_count: int


class MonitoringOverviewResponse(BaseModel):
    generated_at: datetime
    vendors: list[MonitoringOverviewVendorItem]


class MonitoringVendorModelItem(BaseModel):
    model_config_id: int
    model_id: str
    display_name: Optional[str] = None
    fused_status: str
    connection_count: int


class MonitoringVendorResponse(BaseModel):
    generated_at: datetime
    vendor_id: int
    vendor_key: str
    vendor_name: str
    models: list[MonitoringVendorModelItem]


class MonitoringModelResponse(BaseModel):
    generated_at: datetime
    vendor_id: int
    vendor_key: str
    vendor_name: str
    model_config_id: int
    model_id: str
    display_name: Optional[str] = None
    connections: list[MonitoringModelConnectionRow]


class MonitoringManualProbeResponse(BaseModel):
    connection_id: int
    checked_at: datetime
    endpoint_ping_status: str
    endpoint_ping_ms: Optional[int] = None
    conversation_status: str
    conversation_delay_ms: Optional[int] = None
    fused_status: str
    failure_kind: Optional[str] = None
    detail: str


__all__ = [
    "MonitoringConnectionHistoryItem",
    "MonitoringConnectionBaseRow",
    "MonitoringManualProbeResponse",
    "MonitoringModelConnectionRow",
    "MonitoringModelResponse",
    "MonitoringOverviewResponse",
    "MonitoringOverviewVendorItem",
    "MonitoringVendorModelItem",
    "MonitoringVendorResponse",
]
