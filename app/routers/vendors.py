from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.dependencies import get_db
from app.models.models import Vendor
from app.schemas.schemas import VendorCreate, VendorResponse, VendorUpdate

router = APIRouter(prefix="/api/vendors", tags=["vendors"])


def _normalize_vendor_payload(payload: dict[str, object]) -> dict[str, object]:
    normalized = dict(payload)
    key = normalized.get("key")
    if isinstance(key, str):
        normalized["key"] = key.strip().lower()
    name = normalized.get("name")
    if isinstance(name, str):
        normalized["name"] = name.strip()
    description = normalized.get("description")
    if isinstance(description, str):
        normalized["description"] = description.strip() or None
    return normalized


async def _get_vendor_or_404(db: AsyncSession, vendor_id: int) -> Vendor:
    vendor = await db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor


async def _ensure_vendor_uniqueness(
    db: AsyncSession,
    *,
    key: str | None,
    name: str | None,
    exclude_vendor_id: int | None = None,
) -> None:
    filters = []
    if key is not None:
        filters.append(Vendor.key == key)
    if name is not None:
        filters.append(Vendor.name == name)
    if not filters:
        return

    query = select(Vendor).where(or_(*filters))
    if exclude_vendor_id is not None:
        query = query.where(Vendor.id != exclude_vendor_id)

    existing = (await db.execute(query)).scalars().first()
    if existing is None:
        return

    if key is not None and existing.key == key:
        raise HTTPException(
            status_code=409, detail=f"Vendor key '{key}' already exists"
        )
    if name is not None and existing.name == name:
        raise HTTPException(
            status_code=409,
            detail=f"Vendor name '{name}' already exists",
        )


@router.get("", response_model=list[VendorResponse])
async def list_vendors(db: Annotated[AsyncSession, Depends(get_db)]):
    result = await db.execute(select(Vendor).order_by(Vendor.id.asc()))
    return result.scalars().all()


@router.post("", response_model=VendorResponse, status_code=status.HTTP_201_CREATED)
async def create_vendor(
    body: VendorCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    payload = _normalize_vendor_payload(body.model_dump())
    normalized_key = payload.get("key")
    normalized_name = payload.get("name")
    await _ensure_vendor_uniqueness(
        db,
        key=normalized_key if isinstance(normalized_key, str) else None,
        name=normalized_name if isinstance(normalized_name, str) else None,
    )

    vendor = Vendor(**payload)
    db.add(vendor)
    await db.commit()
    await db.refresh(vendor)
    return vendor


@router.get("/{vendor_id}", response_model=VendorResponse)
async def get_vendor(vendor_id: int, db: Annotated[AsyncSession, Depends(get_db)]):
    return await _get_vendor_or_404(db, vendor_id)


@router.patch("/{vendor_id}", response_model=VendorResponse)
async def update_vendor(
    vendor_id: int,
    body: VendorUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    vendor = await _get_vendor_or_404(db, vendor_id)
    update_data = _normalize_vendor_payload(body.model_dump(exclude_unset=True))
    normalized_key = update_data.get("key")
    normalized_name = update_data.get("name")

    await _ensure_vendor_uniqueness(
        db,
        key=normalized_key if isinstance(normalized_key, str) else None,
        name=normalized_name if isinstance(normalized_name, str) else None,
        exclude_vendor_id=vendor.id,
    )

    for key, value in update_data.items():
        setattr(vendor, key, value)
    vendor.updated_at = utc_now()

    await db.commit()
    await db.refresh(vendor)
    return vendor


@router.delete("/{vendor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_vendor(
    vendor_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    vendor = await _get_vendor_or_404(db, vendor_id)
    await db.delete(vendor)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
