# ruff: noqa: F821,F401
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time import utc_now


class Profile(Base):
    __tablename__ = "profiles"
    __table_args__ = (
        Index(
            "uq_profiles_single_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
        Index(
            "uq_profiles_single_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default = true"),
        ),
        Index("idx_profiles_deleted_at", "deleted_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_editable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    model_configs: Mapped[list["ModelConfig"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    endpoints: Mapped[list["Endpoint"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    connections: Mapped[list["Connection"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    user_settings: Mapped[list["UserSetting"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    endpoint_fx_rate_settings: Mapped[list["EndpointFxRateSetting"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    request_logs: Mapped[list["RequestLog"]] = relationship(back_populates="profile")
    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="profile")
    header_blocklist_rules: Mapped[list["HeaderBlocklistRule"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    pricing_templates: Mapped[list["PricingTemplate"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )


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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    model_configs: Mapped[list["ModelConfig"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )
