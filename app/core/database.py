from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import ensure_postgresql_database_url, get_settings


def _create_engine() -> AsyncEngine:
    settings = get_settings()
    ensure_postgresql_database_url(settings.database_url)
    engine_kwargs = {
        "echo": False,
    }
    if settings.app_env == "test":
        engine_kwargs["poolclass"] = NullPool
    else:
        engine_kwargs["pool_pre_ping"] = True
        engine_kwargs["pool_recycle"] = 300
    return create_async_engine(settings.database_url, **engine_kwargs)


engine = _create_engine()
AsyncSessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


def get_engine() -> AsyncEngine:
    return engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return AsyncSessionLocal


class Base(DeclarativeBase):
    pass
