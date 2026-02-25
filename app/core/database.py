from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
    AsyncSession,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from app.core.config import settings


def _make_engine():
    _is_sqlite = settings.database_url.startswith("sqlite")
    return create_async_engine(
        settings.database_url,
        echo=False,
        **(
            {"poolclass": StaticPool}
            if _is_sqlite
            else {"pool_pre_ping": True, "pool_recycle": 300}
        ),
    )


engine = _make_engine()

AsyncSessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


class Base(DeclarativeBase):
    pass
