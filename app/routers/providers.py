from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.models.models import Provider
from app.schemas.schemas import ProviderResponse

router = APIRouter(prefix="/api/providers", tags=["providers"])


@router.get("", response_model=list[ProviderResponse])
async def list_providers(db: Annotated[AsyncSession, Depends(get_db)]):
    result = await db.execute(select(Provider).order_by(Provider.id))
    return result.scalars().all()


@router.get("/{provider_id}", response_model=ProviderResponse)
async def get_provider(provider_id: int, db: Annotated[AsyncSession, Depends(get_db)]):
    result = await db.execute(select(Provider).where(Provider.id == provider_id))
    provider = result.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    return provider
