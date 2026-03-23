# ruff: noqa: F821,F401
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time import utc_now

if TYPE_CHECKING:
    from app.models.domains.identity import Profile, Provider


class RequestLog(Base):
    __tablename__ = "request_logs"
    __table_args__ = (
        Index("idx_request_logs_billable_flag", "billable_flag"),
        Index("idx_request_logs_priced_flag", "priced_flag"),
        Index("idx_request_logs_profile_created_at", "profile_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    model_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    provider_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    endpoint_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    connection_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )
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
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_cost_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_cost_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
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
    pricing_snapshot_reasoning: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    cache_read_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_creation_input_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    cache_read_input_cost_micros: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    cache_creation_input_cost_micros: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    pricing_snapshot_cache_read_input: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    pricing_snapshot_cache_creation_input: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    pricing_snapshot_missing_special_token_price_policy: Mapped[str | None] = (
        mapped_column(String(20), nullable=True)
    )
    pricing_config_version_used: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    request_path: Mapped[str] = mapped_column(String(500), nullable=False)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    endpoint_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )

    profile: Mapped["Profile"] = relationship(back_populates="request_logs")


class UserSetting(Base):
    __tablename__ = "user_settings"
    __table_args__ = (
        UniqueConstraint("profile_id", name="uq_user_settings_profile_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_currency_code: Mapped[str] = mapped_column(
        String(3), default="USD", nullable=False
    )
    report_currency_symbol: Mapped[str] = mapped_column(
        String(5), default="$", nullable=False
    )
    timezone_preference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    profile: Mapped["Profile"] = relationship(back_populates="user_settings")


class EndpointFxRateSetting(Base):
    __tablename__ = "endpoint_fx_rate_settings"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "model_id",
            "endpoint_id",
            name="uq_fx_profile_model_endpoint",
        ),
        Index("idx_fx_endpoint_id", "endpoint_id"),
        Index(
            "idx_fx_profile_model_endpoint",
            "profile_id",
            "model_id",
            "endpoint_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    endpoint_id: Mapped[int] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False
    )
    fx_rate: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    profile: Mapped["Profile"] = relationship(
        back_populates="endpoint_fx_rate_settings"
    )


class HeaderBlocklistRule(Base):
    __tablename__ = "header_blocklist_rules"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "match_type",
            "pattern",
            name="uq_hbr_profile_match_pattern",
        ),
        Index(
            "uq_hbr_system_match_pattern",
            "match_type",
            "pattern",
            unique=True,
            postgresql_where=text("is_system = true"),
        ),
        Index("idx_hbr_enabled", "enabled"),
        CheckConstraint(
            "((is_system = true AND profile_id IS NULL) OR (is_system = false AND profile_id IS NOT NULL))",
            name="ck_hbr_profile_scope",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    match_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # "exact" or "prefix"
    pattern: Mapped[str] = mapped_column(
        String(200), nullable=False
    )  # normalized lowercase
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    profile: Mapped["Profile | None"] = relationship(
        back_populates="header_blocklist_rules"
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("idx_audit_logs_connection_id", "connection_id"),
        Index("idx_audit_logs_profile_created_at", "profile_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
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
    connection_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )
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
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )

    profile: Mapped["Profile"] = relationship(back_populates="audit_logs")


class LoadbalanceEvent(Base):
    __tablename__ = "loadbalance_events"
    __table_args__ = (
        Index("idx_loadbalance_events_profile_created", "profile_id", "created_at"),
        Index("idx_loadbalance_events_connection", "connection_id", "created_at"),
        Index("idx_loadbalance_events_event_type", "event_type"),
        CheckConstraint(
            "event_type IN ('opened', 'extended', 'probe_eligible', 'recovered', 'not_opened')",
            name="chk_event_type",
        ),
        CheckConstraint(
            "failure_kind IN ('transient_http', 'auth_like', 'connect_error', 'timeout') OR failure_kind IS NULL",
            name="chk_failure_kind",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    connection_id: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    failure_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False)
    cooldown_seconds: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    blocked_until_mono: Mapped[float | None] = mapped_column(
        Numeric(20, 6), nullable=True
    )
    model_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    endpoint_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("providers.id"), nullable=True
    )
    failure_threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    backoff_multiplier: Mapped[float | None] = mapped_column(
        Numeric(5, 2), nullable=True
    )
    max_cooldown_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )

    profile: Mapped["Profile"] = relationship(back_populates="loadbalance_events")
    provider: Mapped["Provider"] = relationship()


class LoadbalanceCurrentState(Base):
    __tablename__ = "loadbalance_current_state"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "connection_id",
            name="uq_loadbalance_current_state_profile_connection",
        ),
        Index(
            "idx_loadbalance_current_state_profile_connection",
            "profile_id",
            "connection_id",
        ),
        CheckConstraint(
            "last_failure_kind IN ('transient_http', 'auth_like', 'connect_error', 'timeout') OR last_failure_kind IS NULL",
            name="chk_loadbalance_current_state_failure_kind",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    last_failure_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_cooldown_seconds: Mapped[float] = mapped_column(
        Numeric(10, 2), nullable=False, default=0
    )
    blocked_until_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    probe_eligible_logged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    profile: Mapped["Profile"] = relationship(
        back_populates="loadbalance_current_states"
    )
