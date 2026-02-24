from datetime import datetime

from sqlalchemy import (
    ForeignKey,
    String,
    Boolean,
    Integer,
    BigInteger,
    DateTime,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    provider_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # openai, anthropic, gemini
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    audit_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    audit_capture_bodies: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    model_configs: Mapped[list["ModelConfig"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )


class ModelConfig(Base):
    __tablename__ = "model_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id"), nullable=False)
    model_id: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    model_type: Mapped[str] = mapped_column(
        String(20), default="native", nullable=False
    )  # native, proxy
    redirect_to: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )  # target model_id for redirect models
    lb_strategy: Mapped[str] = mapped_column(
        String(50), default="single", nullable=False
    )  # single, failover
    failover_recovery_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    failover_recovery_cooldown_seconds: Mapped[int] = mapped_column(
        Integer, default=60, nullable=False
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    provider: Mapped["Provider"] = relationship(back_populates="model_configs")
    endpoints: Mapped[list["Endpoint"]] = relationship(
        back_populates="model_config_rel", cascade="all, delete-orphan"
    )


class Endpoint(Base):
    __tablename__ = "endpoints"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_config_id: Mapped[int] = mapped_column(
        ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False
    )
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key: Mapped[str] = mapped_column(String(500), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # null=use provider default; "openai", "anthropic" to override
    custom_headers: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON object of custom HTTP headers
    health_status: Mapped[str] = mapped_column(
        String(20), default="unknown", nullable=False
    )  # unknown, healthy, unhealthy
    health_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_health_check: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pricing_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    pricing_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    pricing_currency_code: Mapped[str | None] = mapped_column(String(3), nullable=True)
    input_price: Mapped[str | None] = mapped_column(String(20), nullable=True)
    output_price: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cached_input_price: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reasoning_price: Mapped[str | None] = mapped_column(String(20), nullable=True)
    missing_special_token_policy: Mapped[str] = mapped_column(
        String(20), default="MAP_TO_OUTPUT", nullable=False
    )
    pricing_config_version: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    model_config_rel: Mapped["ModelConfig"] = relationship(back_populates="endpoints")


class RequestLog(Base):
    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    provider_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    endpoint_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    endpoint_base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    response_time_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    is_stream: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success_flag: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    billable_flag: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    priced_flag: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    unpriced_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_cost_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_cost_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cached_input_cost_micros: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    reasoning_cost_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_cost_original_micros: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    total_cost_user_currency_micros: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    currency_code_original: Mapped[str | None] = mapped_column(String(3), nullable=True)
    report_currency_code: Mapped[str | None] = mapped_column(String(3), nullable=True)
    report_currency_symbol: Mapped[str | None] = mapped_column(String(5), nullable=True)
    fx_rate_used: Mapped[str | None] = mapped_column(String(20), nullable=True)
    fx_rate_source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    pricing_snapshot_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    pricing_snapshot_input: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    pricing_snapshot_output: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    pricing_snapshot_cached_input: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    pricing_snapshot_reasoning: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    pricing_snapshot_policy: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    pricing_config_version_used: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    request_path: Mapped[str] = mapped_column(String(500), nullable=False)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    endpoint_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True
    )


class UserSetting(Base):
    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_currency_code: Mapped[str] = mapped_column(
        String(3), default="USD", nullable=False
    )
    report_currency_symbol: Mapped[str] = mapped_column(
        String(5), default="$", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class EndpointFxRateSetting(Base):
    __tablename__ = "endpoint_fx_rate_settings"
    __table_args__ = (
        UniqueConstraint("model_id", "endpoint_id", name="uq_fx_model_endpoint"),
        Index("idx_fx_endpoint_id", "endpoint_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    endpoint_id: Mapped[int] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False
    )
    fx_rate: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class HeaderBlocklistRule(Base):
    __tablename__ = "header_blocklist_rules"
    __table_args__ = (
        UniqueConstraint("match_type", "pattern", name="uq_match_type_pattern"),
        Index("idx_hbr_enabled", "enabled"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    match_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # "exact" or "prefix"
    pattern: Mapped[str] = mapped_column(
        String(200), nullable=False
    )  # normalized lowercase
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_log_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("request_logs.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )
    provider_id: Mapped[int] = mapped_column(
        ForeignKey("providers.id"), nullable=False, index=True
    )
    model_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    endpoint_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    endpoint_base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    endpoint_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_method: Mapped[str] = mapped_column(String(10), nullable=False)
    request_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    request_headers: Mapped[str] = mapped_column(Text, nullable=False)
    request_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    response_headers: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_stream: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True
    )
