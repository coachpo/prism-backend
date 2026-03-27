from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.dependencies import get_db
from app.models.models import ModelConfig, Profile, Vendor
from app.schemas.schemas import (
    VendorCreate,
    VendorDeleteConflictDetail,
    VendorModelUsageItem,
    VendorResponse,
    VendorUpdate,
)

router = APIRouter(prefix="/api/vendors", tags=["vendors"])
_VENDOR_DELETE_IN_USE_MESSAGE = "Cannot delete vendor that is referenced by models"


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
    icon_key = normalized.get("icon_key")
    if isinstance(icon_key, str):
        normalized["icon_key"] = icon_key.strip().lower() or None
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
    if key is not None and name is not None:
        query = select(Vendor).where((Vendor.key == key) | (Vendor.name == name))
    elif key is not None:
        query = select(Vendor).where(Vendor.key == key)
    elif name is not None:
        query = select(Vendor).where(Vendor.name == name)
    else:
        return

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


async def _list_vendor_model_usage_rows(
    db: AsyncSession, *, vendor_id: int
) -> list[VendorModelUsageItem]:
    result = await db.execute(
        select(
            ModelConfig.id.label("model_config_id"),
            Profile.id.label("profile_id"),
            Profile.name.label("profile_name"),
            ModelConfig.model_id,
            ModelConfig.display_name,
            ModelConfig.model_type,
            ModelConfig.api_family,
            ModelConfig.is_enabled,
        )
        .join(Profile, Profile.id == ModelConfig.profile_id)
        .where(ModelConfig.vendor_id == vendor_id)
        .order_by(Profile.id.asc(), ModelConfig.id.asc())
    )
    return [VendorModelUsageItem.model_validate(row) for row in result.mappings().all()]


def _build_vendor_delete_conflict_detail(
    models: list[VendorModelUsageItem],
    *,
    message: str,
) -> dict[str, object]:
    return VendorDeleteConflictDetail(
        message=message,
        models=models,
    ).model_dump(mode="json")


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


@router.get("/{vendor_id}/models", response_model=list[VendorModelUsageItem])
async def list_vendor_models(
    vendor_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _ = await _get_vendor_or_404(db, vendor_id)
    return await _list_vendor_model_usage_rows(db, vendor_id=vendor_id)


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

    usage_rows = await _list_vendor_model_usage_rows(db, vendor_id=vendor_id)
    if usage_rows:
        raise HTTPException(
            status_code=409,
            detail=_build_vendor_delete_conflict_detail(
                usage_rows,
                message=_VENDOR_DELETE_IN_USE_MESSAGE,
            ),
        )

    await db.delete(vendor)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        usage_rows = await _list_vendor_model_usage_rows(db, vendor_id=vendor_id)
        raise HTTPException(
            status_code=409,
            detail=_build_vendor_delete_conflict_detail(
                usage_rows,
                message=_VENDOR_DELETE_IN_USE_MESSAGE,
            ),
        ) from exc

    return Response(status_code=status.HTTP_204_NO_CONTENT)
