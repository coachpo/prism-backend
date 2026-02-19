from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./gateway.db"
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


settings = Settings()
