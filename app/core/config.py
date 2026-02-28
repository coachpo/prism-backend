from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://prism:prism@localhost:5432/prism"
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

    class Config:
        env_file = ".env"


def ensure_postgresql_database_url(database_url: str) -> None:
    if not database_url.lower().startswith("postgresql"):
        raise ValueError(
            "DATABASE_URL must be a PostgreSQL DSN, for example "
            "postgresql+asyncpg://prism:prism@localhost:5432/prism"
        )


settings = Settings()
