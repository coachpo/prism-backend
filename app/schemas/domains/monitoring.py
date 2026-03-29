from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MonitoringOverviewVendorItem(BaseModel):
    vendor_id: int
    vendor_key: str
    vendor_name: str
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
    display_name: str | None = None
    fused_status: str
    connection_count: int


class MonitoringVendorResponse(BaseModel):
    generated_at: datetime
    vendor_id: int
    vendor_key: str
    vendor_name: str
    models: list[MonitoringVendorModelItem]


class MonitoringConnectionHistoryItem(BaseModel):
    checked_at: datetime
    endpoint_ping_status: str
    endpoint_ping_ms: int | None = None
    conversation_status: str
    conversation_delay_ms: int | None = None
    failure_kind: str | None = None


class MonitoringConnectionRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    connection_id: int
    endpoint_id: int
    endpoint_name: str
    endpoint_ping_status: str
    endpoint_ping_ms: int | None = None
    conversation_status: str
    conversation_delay_ms: int | None = None
    fused_status: str
    recent_history: list[MonitoringConnectionHistoryItem]


class MonitoringModelResponse(BaseModel):
    generated_at: datetime
    vendor_id: int
    vendor_key: str
    vendor_name: str
    model_config_id: int
    model_id: str
    display_name: str | None = None
    connections: list[MonitoringConnectionRow]


class MonitoringManualProbeResponse(BaseModel):
    connection_id: int
    checked_at: datetime
    endpoint_ping_status: str
    endpoint_ping_ms: int | None = None
    conversation_status: str
    conversation_delay_ms: int | None = None
    fused_status: str
    failure_kind: str | None = None
    detail: str


__all__ = [
    "MonitoringConnectionHistoryItem",
    "MonitoringConnectionRow",
    "MonitoringManualProbeResponse",
    "MonitoringModelResponse",
    "MonitoringOverviewResponse",
    "MonitoringOverviewVendorItem",
    "MonitoringVendorModelItem",
    "MonitoringVendorResponse",
]
