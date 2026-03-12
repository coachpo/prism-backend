import os
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = Field(default="", min_length=1)
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"
    app_env: Literal["development", "test", "production"] = "development"
    cors_allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # Timeout settings for upstream LLM requests
    connect_timeout: float = 10.0
    read_timeout: float = 120.0
    write_timeout: float = 30.0
    # Load balancer settings
    failover_cooldown_seconds: int = 60
    max_retries: int = 3
    failover_failure_threshold: int = Field(default=2, ge=1, le=10)
    failover_backoff_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    failover_max_cooldown_seconds: int = Field(default=900, ge=1, le=86_400)
    failover_jitter_ratio: float = Field(default=0.2, ge=0.0, le=1.0)
    failover_auth_error_cooldown_seconds: int = Field(default=1800, ge=1, le=86_400)
    auth_jwt_secret: str = "prism-dev-jwt-secret-change-me-2026"
    secret_encryption_key: str = "prism-dev-encryption-key-change-me"
    auth_access_token_ttl_seconds: int = Field(default=900, ge=60, le=86_400)
    auth_refresh_token_ttl_seconds: int = Field(default=604800, ge=300, le=31_536_000)
    auth_reset_code_ttl_seconds: int = Field(default=600, ge=60, le=86_400)
    auth_cookie_name: str = "prism_access_token"
    auth_refresh_cookie_name: str = "prism_refresh_token"
    auth_cookie_secure: bool = False
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_sender_email: str | None = None
    smtp_sender_name: str = "Prism"
    smtp_use_tls: bool = True
    webauthn_rp_id: str = Field(default="localhost", min_length=1)
    webauthn_rp_name: str = Field(default="Prism LLM Gateway", min_length=1)
    webauthn_origin: str = Field(default="http://localhost:5173", min_length=1)

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.cors_allowed_origins.split(",")
            if origin.strip()
        ]

    @property
    def docs_enabled(self) -> bool:
        return self.app_env != "production"


def ensure_postgresql_database_url(database_url: str) -> None:
    if not database_url.lower().startswith("postgresql"):
        raise ValueError(
            "DATABASE_URL must be a PostgreSQL DSN, for example "
            "postgresql+asyncpg://<user>:<password>@<host>:<port>/<database>"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(database_url=os.getenv("DATABASE_URL", ""))
