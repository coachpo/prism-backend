from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
 )
from sqlalchemy.orm import DeclarativeBase

from app.core.config import ensure_postgresql_database_url, get_settings


def _create_engine() -> AsyncEngine:
    settings = get_settings()
    ensure_postgresql_database_url(settings.database_url)
    return create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
        pool_recycle=300,
    )


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
