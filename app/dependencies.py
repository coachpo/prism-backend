import asyncio
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except asyncio.CancelledError:
            pass
        except Exception:
            await session.rollback()
            raise
