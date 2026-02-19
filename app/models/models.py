from datetime import datetime

from sqlalchemy import ForeignKey, String, Boolean, Integer, DateTime, Text
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
    )  # native, redirect
    redirect_to: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )  # target model_id for redirect models
    lb_strategy: Mapped[str] = mapped_column(
        String(50), default="single", nullable=False
    )  # single, round_robin, failover
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
    health_status: Mapped[str] = mapped_column(
        String(20), default="unknown", nullable=False
    )  # unknown, healthy, unhealthy
    last_health_check: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    model_config_rel: Mapped["ModelConfig"] = relationship(back_populates="endpoints")
