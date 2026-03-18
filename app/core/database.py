from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import ensure_postgresql_database_url, get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _create_engine() -> AsyncEngine:
    settings = get_settings()
    ensure_postgresql_database_url(settings.database_url)
    engine_kwargs: dict[str, object] = {}
    engine_kwargs["echo"] = False
    if settings.app_env == "test":
        engine_kwargs["poolclass"] = NullPool
    else:
        engine_kwargs["pool_pre_ping"] = True
        engine_kwargs["pool_recycle"] = 300
    return create_async_engine(settings.database_url, **engine_kwargs)


def get_engine() -> AsyncEngine:
    global _engine

    if _engine is None:
        _engine = _create_engine()

    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory

    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )

    return _session_factory


def AsyncSessionLocal() -> AsyncSession:
    return get_session_factory()()


class Base(DeclarativeBase):
    pass
