from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"
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
    failover_auth_error_cooldown_seconds: int = Field(
        default=1800, ge=1, le=86_400
    )

def ensure_postgresql_database_url(database_url: str) -> None:
    if not database_url.lower().startswith("postgresql"):
        raise ValueError(
            "DATABASE_URL must be a PostgreSQL DSN, for example "
            "postgresql+asyncpg://<user>:<password>@<host>:<port>/<database>"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
