from __future__ import annotations

from typing import Any

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
from sqlalchemy.dialects.postgresql import ARRAY
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

    model_configs: Mapped[list[Any]] = relationship(
        "ModelConfig", back_populates="profile", cascade="all, delete-orphan"
    )
    endpoints: Mapped[list[Any]] = relationship(
        "Endpoint", back_populates="profile", cascade="all, delete-orphan"
    )
    connections: Mapped[list[Any]] = relationship(
        "Connection", back_populates="profile", cascade="all, delete-orphan"
    )
    user_settings: Mapped[list[Any]] = relationship(
        "UserSetting", back_populates="profile", cascade="all, delete-orphan"
    )
    endpoint_fx_rate_settings: Mapped[list[Any]] = relationship(
        "EndpointFxRateSetting", back_populates="profile", cascade="all, delete-orphan"
    )
    request_logs: Mapped[list[Any]] = relationship(
        "RequestLog", back_populates="profile"
    )
    usage_request_events: Mapped[list[Any]] = relationship(
        "UsageRequestEvent", back_populates="profile"
    )
    audit_logs: Mapped[list[Any]] = relationship("AuditLog", back_populates="profile")
    header_blocklist_rules: Mapped[list[Any]] = relationship(
        "HeaderBlocklistRule", back_populates="profile", cascade="all, delete-orphan"
    )
    pricing_templates: Mapped[list[Any]] = relationship(
        "PricingTemplate", back_populates="profile", cascade="all, delete-orphan"
    )
    loadbalance_events: Mapped[list[Any]] = relationship(
        "LoadbalanceEvent", back_populates="profile", cascade="all, delete-orphan"
    )
    loadbalance_current_states: Mapped[list[Any]] = relationship(
        "LoadbalanceCurrentState",
        back_populates="profile",
        cascade="all, delete-orphan",
    )


class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
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

    model_configs: Mapped[list[Any]] = relationship(
        "ModelConfig", back_populates="vendor"
    )


class AppAuthSettings(Base):
    __tablename__ = "app_auth_settings"
    __table_args__ = (
        UniqueConstraint("singleton_key", name="uq_app_auth_settings_singleton_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    singleton_key: Mapped[str] = mapped_column(
        String(20), default="app", nullable=False
    )
    auth_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    username: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    pending_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_bound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    email_verification_code_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    email_verification_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    email_verification_attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("idx_refresh_tokens_revoked_at", "revoked_at"),
        Index("idx_refresh_tokens_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    auth_subject_id: Mapped[int] = mapped_column(
        ForeignKey("app_auth_settings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    session_duration: Mapped[str] = mapped_column(
        String(20), default="7_days", nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    rotated_from_id: Mapped[int | None] = mapped_column(
        ForeignKey("refresh_tokens.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class ProxyApiKey(Base):
    __tablename__ = "proxy_api_keys"
    __table_args__ = (
        UniqueConstraint("key_prefix", name="uq_proxy_api_keys_prefix"),
        Index("idx_proxy_api_keys_is_active", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(200), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    last_four: Mapped[str] = mapped_column(String(4), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_ip: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_by_auth_subject_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_auth_settings.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    rotated_from_id: Mapped[int | None] = mapped_column(
        ForeignKey("proxy_api_keys.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class PasswordResetChallenge(Base):
    __tablename__ = "password_reset_challenges"
    __table_args__ = (
        Index("idx_password_reset_challenges_expires_at", "expires_at"),
        Index("idx_password_reset_challenges_consumed_at", "consumed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    auth_subject_id: Mapped[int] = mapped_column(
        ForeignKey("app_auth_settings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    otp_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    requested_ip: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class WebAuthnChallenge(Base):
    __tablename__ = "webauthn_challenges"
    __table_args__ = (
        Index("idx_webauthn_challenges_expires_at", "expires_at"),
        Index("idx_webauthn_challenges_challenge_key", "challenge_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    challenge_key: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, index=True
    )
    challenge: Mapped[bytes] = mapped_column("challenge", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class WebAuthnCredential(Base):
    __tablename__ = "webauthn_credentials"
    __table_args__ = (
        Index("idx_webauthn_credentials_auth_subject", "auth_subject_id"),
        Index("idx_webauthn_credentials_last_used", "last_used_at"),
        UniqueConstraint("credential_id", name="uq_credential_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    auth_subject_id: Mapped[int] = mapped_column(
        ForeignKey("app_auth_settings.id", ondelete="CASCADE"),
        nullable=False,
    )

    # WebAuthn core fields
    credential_id: Mapped[bytes] = mapped_column("credential_id", nullable=False)
    public_key: Mapped[bytes] = mapped_column("public_key", nullable=False)
    sign_count: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False, server_default="0"
    )

    # Device management
    device_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    aaguid: Mapped[bytes | None] = mapped_column("aaguid", nullable=True)
    transports: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)

    # Backup and sync identifiers
    backup_eligible: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, server_default="false"
    )
    backup_state: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, server_default="false"
    )

    # Audit fields
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        server_default=text("NOW()"),
    )
