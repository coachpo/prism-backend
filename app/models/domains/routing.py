from __future__ import annotations

# ruff: noqa: F821,F401
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.crypto import decrypt_secret, mask_secret
from app.core.database import Base
from app.core.time import utc_now

if TYPE_CHECKING:
    from app.models.domains.identity import Profile, Provider


_UNREADABLE_SECRET_MASK = "********"


class LoadbalanceStrategy(Base):
    __tablename__ = "loadbalance_strategies"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "name",
            name="uq_loadbalance_strategies_profile_name",
        ),
        UniqueConstraint(
            "profile_id",
            "id",
            name="uq_loadbalance_strategies_profile_id_id",
        ),
        CheckConstraint(
            "strategy_type IN ('single', 'failover')",
            name="chk_loadbalance_strategies_type",
        ),
        CheckConstraint(
            "strategy_type = 'failover' OR failover_recovery_enabled = false",
            name="chk_loadbalance_strategies_recovery",
        ),
        Index("idx_loadbalance_strategies_profile_id", "profile_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    strategy_type: Mapped[str] = mapped_column(
        String(20), default="single", nullable=False
    )
    failover_recovery_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    failover_cooldown_seconds: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    failover_failure_threshold: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    failover_backoff_multiplier: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    failover_max_cooldown_seconds: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    failover_jitter_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    failover_auth_error_cooldown_seconds: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    profile: Mapped["Profile"] = relationship()
    model_configs: Mapped[list["ModelConfig"]] = relationship(
        back_populates="loadbalance_strategy", overlaps="model_configs,profile"
    )


class ModelConfig(Base):
    __tablename__ = "model_configs"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "model_id",
            name="uq_model_configs_profile_model_id",
        ),
        Index(
            "idx_model_configs_profile_model_enabled",
            "profile_id",
            "model_id",
            "is_enabled",
        ),
        Index("idx_model_configs_loadbalance_strategy_id", "loadbalance_strategy_id"),
        ForeignKeyConstraint(
            ["profile_id", "loadbalance_strategy_id"],
            ["loadbalance_strategies.profile_id", "loadbalance_strategies.id"],
            name="fk_model_configs_profile_loadbalance_strategy",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(model_type = 'native' AND loadbalance_strategy_id IS NOT NULL) OR "
            + "(model_type = 'proxy' AND loadbalance_strategy_id IS NULL)",
            name="chk_model_configs_strategy_attachment",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id"), nullable=False)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    model_type: Mapped[str] = mapped_column(
        String(20), default="native", nullable=False
    )  # native, proxy
    redirect_to: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )  # target model_id for redirect models
    loadbalance_strategy_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    profile: Mapped["Profile"] = relationship(
        back_populates="model_configs", overlaps="model_configs"
    )
    provider: Mapped["Provider"] = relationship(back_populates="model_configs")
    loadbalance_strategy: Mapped["LoadbalanceStrategy | None"] = relationship(
        back_populates="model_configs", overlaps="model_configs,profile"
    )
    connections: Mapped[list["Connection"]] = relationship(
        back_populates="model_config_rel", cascade="all, delete-orphan"
    )


class Endpoint(Base):
    __tablename__ = "endpoints"
    __table_args__ = (
        UniqueConstraint("profile_id", "name", name="uq_endpoints_profile_name"),
        Index("idx_endpoints_profile_position", "profile_id", "position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key: Mapped[str] = mapped_column(String(500), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    profile: Mapped["Profile"] = relationship(back_populates="endpoints")
    connections: Mapped[list["Connection"]] = relationship(
        back_populates="endpoint_rel"
    )

    @property
    def has_api_key(self) -> bool:
        if not self.api_key.strip():
            return False
        try:
            return bool(decrypt_secret(self.api_key))
        except ValueError:
            return True

    @property
    def masked_api_key(self) -> str | None:
        if not self.api_key.strip():
            return None
        try:
            return mask_secret(self.api_key)
        except ValueError:
            return _UNREADABLE_SECRET_MASK


class PricingTemplate(Base):
    __tablename__ = "pricing_templates"
    __table_args__ = (
        UniqueConstraint(
            "profile_id", "name", name="uq_pricing_templates_profile_name"
        ),
        Index("idx_pricing_templates_profile_id", "profile_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    pricing_unit: Mapped[str] = mapped_column(
        String(20), default="PER_1M", nullable=False
    )
    pricing_currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    input_price: Mapped[str] = mapped_column(String(20), nullable=False)
    output_price: Mapped[str] = mapped_column(String(20), nullable=False)
    cached_input_price: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cache_creation_price: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reasoning_price: Mapped[str | None] = mapped_column(String(20), nullable=True)
    missing_special_token_price_policy: Mapped[str] = mapped_column(
        String(20), default="MAP_TO_OUTPUT", nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    profile: Mapped["Profile"] = relationship(back_populates="pricing_templates")
    connections: Mapped[list["Connection"]] = relationship(
        back_populates="pricing_template_rel"
    )


class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = (
        Index("idx_connections_model_config_id", "model_config_id"),
        Index("idx_connections_endpoint_id", "endpoint_id"),
        Index("idx_connections_is_active", "is_active"),
        Index("idx_connections_priority", "priority"),
        Index("idx_connections_profile_id", "profile_id"),
        Index("idx_connections_pricing_template_id", "pricing_template_id"),
        Index(
            "idx_connections_profile_model_active_priority",
            "profile_id",
            "model_config_id",
            "is_active",
            "priority",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_config_id: Mapped[int] = mapped_column(
        ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False
    )
    endpoint_id: Mapped[int] = mapped_column(
        ForeignKey("endpoints.id", ondelete="RESTRICT"), nullable=False
    )
    pricing_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("pricing_templates.id", ondelete="RESTRICT"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    last_health_check: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    profile: Mapped["Profile"] = relationship(back_populates="connections")
    model_config_rel: Mapped["ModelConfig"] = relationship(back_populates="connections")
    endpoint_rel: Mapped["Endpoint"] = relationship(back_populates="connections")
    pricing_template_rel: Mapped["PricingTemplate | None"] = relationship(
        back_populates="connections"
    )

    @property
    def base_url(self) -> str | None:
        if self.endpoint_rel is None:
            return None
        return self.endpoint_rel.base_url

    @property
    def api_key(self) -> str | None:
        if self.endpoint_rel is None:
            return None
        try:
            return decrypt_secret(self.endpoint_rel.api_key)
        except ValueError:
            return None
