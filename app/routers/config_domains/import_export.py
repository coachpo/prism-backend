from typing import Annotated
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.dependencies import (
    get_db,
    get_effective_profile_id,
    register_after_commit_action,
)
from app.models.models import Vendor
from app.schemas.schemas import (
    ConfigImportPreviewResponse,
    ConfigImportRequest,
    ConfigImportResponse,
    ConfigVendorCatalogExportResponse,
    ConfigVendorCatalogImportPreviewResponse,
    ConfigVendorCatalogImportRequest,
    ConfigVendorCatalogImportResponse,
    ConfigVendorExport,
)
from app.services.monitoring_service import enqueue_connection_probe

from .export_builder import build_export_payload
from .import_executor import build_import_preview, execute_import_payload
from .import_validator import validate_import_payload

logger = logging.getLogger(__name__)

router = APIRouter()
_validate_import = validate_import_payload


def _enqueue_import_connection_probes(
    *, profile_id: int, connection_ids: tuple[int, ...]
) -> None:
    for connection_id in connection_ids:
        enqueue_connection_probe(
            profile_id=profile_id,
            connection_id=connection_id,
        )


def _build_preview_error_response(
    *, data: ConfigImportRequest, detail: str
) -> ConfigImportPreviewResponse:
    return ConfigImportPreviewResponse(
        ready=False,
        version=2,
        bundle_kind="profile_config",
        endpoints_imported=len(data.endpoints),
        pricing_templates_imported=len(data.pricing_templates),
        strategies_imported=len(data.loadbalance_strategies),
        models_imported=len(data.models),
        connections_imported=sum(len(model.connections) for model in data.models),
        vendor_resolutions=[],
        secret_key_id=data.secret_payload.key_id,
        decryptable_secret_refs=[],
        blocking_errors=[detail],
        warnings=[],
    )


@router.get("/profile/export")
async def export_profile_config(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    data = await build_export_payload(db, profile_id=profile_id)
    date_str = data.exported_at.strftime("%Y-%m-%d")
    return JSONResponse(
        content=data.model_dump(mode="json"),
        headers={
            "Content-Disposition": f'attachment; filename="gateway-config-{date_str}.json"'
        },
    )


@router.post("/profile/import/preview", response_model=ConfigImportPreviewResponse)
async def preview_profile_import(
    data: ConfigImportRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        validate_import_payload(data)
        return await build_import_preview(db, data=data)
    except HTTPException as exc:
        return _build_preview_error_response(data=data, detail=str(exc.detail))


@router.post("/profile/import", response_model=ConfigImportResponse)
async def import_profile_config(
    data: ConfigImportRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    validate_import_payload(data)
    result = await execute_import_payload(db, profile_id=profile_id, data=data)
    if result.imported_connection_ids:
        register_after_commit_action(
            db,
            lambda profile_id=profile_id,
            connection_ids=result.imported_connection_ids: _enqueue_import_connection_probes(
                profile_id=profile_id,
                connection_ids=connection_ids,
            ),
        )
    return result.response


@router.get("/vendors/export", response_model=ConfigVendorCatalogExportResponse)
async def export_vendor_catalog(
    db: Annotated[AsyncSession, Depends(get_db)],
):
    vendors = (
        (await db.execute(select(Vendor).order_by(Vendor.key.asc(), Vendor.id.asc())))
        .scalars()
        .all()
    )
    return ConfigVendorCatalogExportResponse(
        version=2,
        bundle_kind="vendor_catalog",
        exported_at=utc_now(),
        vendors=[
            ConfigVendorExport(
                key=vendor.key,
                name=vendor.name,
                description=vendor.description,
                icon_key=vendor.icon_key,
                audit_enabled=vendor.audit_enabled,
                audit_capture_bodies=vendor.audit_capture_bodies,
            )
            for vendor in vendors
        ],
    )


async def _count_vendor_catalog_changes(
    db: AsyncSession, *, data: ConfigVendorCatalogImportRequest
) -> tuple[int, int, list[str], dict[str, Vendor]]:
    seen_keys: set[str] = set()
    seen_names: dict[str, str] = {}
    blocking_errors: list[str] = []
    for vendor in data.vendors:
        if vendor.key in seen_keys:
            blocking_errors.append(
                f"Vendor catalog bundle contains duplicate vendor key '{vendor.key}'"
            )
        seen_keys.add(vendor.key)

        duplicate_key = seen_names.get(vendor.name)
        if duplicate_key is not None and duplicate_key != vendor.key:
            blocking_errors.append(
                f"Vendor catalog bundle contains duplicate vendor name '{vendor.name}' for keys '{duplicate_key}' and '{vendor.key}'"
            )
        else:
            seen_names[vendor.name] = vendor.key

    existing_vendors = (
        (
            await db.execute(
                select(Vendor).where(
                    Vendor.key.in_([vendor.key for vendor in data.vendors])
                    | Vendor.name.in_([vendor.name for vendor in data.vendors])
                )
            )
        )
        .scalars()
        .all()
    )
    existing_by_key = {vendor.key: vendor for vendor in existing_vendors}
    existing_by_name = {vendor.name: vendor for vendor in existing_vendors}
    create_count = 0
    update_count = 0
    for vendor_data in data.vendors:
        existing = existing_by_key.get(vendor_data.key)
        existing_name_vendor = existing_by_name.get(vendor_data.name)
        if existing is None:
            if (
                existing_name_vendor is not None
                and existing_name_vendor.key != vendor_data.key
            ):
                blocking_errors.append(
                    f"Vendor catalog import would create vendor key '{vendor_data.key}' with name '{vendor_data.name}' that already exists on key '{existing_name_vendor.key}'"
                )
                continue
            create_count += 1
            continue
        if (
            existing_name_vendor is not None
            and existing_name_vendor.key != existing.key
        ):
            blocking_errors.append(
                f"Vendor catalog import would update key '{vendor_data.key}' to duplicate existing vendor name '{vendor_data.name}' used by key '{existing_name_vendor.key}'"
            )
            continue
        if (
            existing.name != vendor_data.name
            or existing.description != vendor_data.description
            or existing.icon_key != vendor_data.icon_key
            or existing.audit_enabled != vendor_data.audit_enabled
            or existing.audit_capture_bodies != vendor_data.audit_capture_bodies
        ):
            update_count += 1
    return create_count, update_count, blocking_errors, existing_by_key


@router.post(
    "/vendors/import/preview",
    response_model=ConfigVendorCatalogImportPreviewResponse,
)
async def preview_vendor_catalog_import(
    data: ConfigVendorCatalogImportRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    (
        create_count,
        update_count,
        blocking_errors,
        _,
    ) = await _count_vendor_catalog_changes(db, data=data)
    return ConfigVendorCatalogImportPreviewResponse(
        ready=len(blocking_errors) == 0,
        version=2,
        bundle_kind="vendor_catalog",
        create_count=create_count,
        update_count=update_count,
        blocking_errors=blocking_errors,
        warnings=[],
    )


@router.post("/vendors/import", response_model=ConfigVendorCatalogImportResponse)
async def import_vendor_catalog(
    data: ConfigVendorCatalogImportRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _, _, blocking_errors, existing_by_key = await _count_vendor_catalog_changes(
        db, data=data
    )
    if blocking_errors:
        raise HTTPException(status_code=400, detail=blocking_errors[0])
    created_count = 0
    updated_count = 0

    for vendor_data in data.vendors:
        existing = existing_by_key.get(vendor_data.key)
        if existing is None:
            db.add(
                Vendor(
                    key=vendor_data.key,
                    name=vendor_data.name,
                    description=vendor_data.description,
                    icon_key=vendor_data.icon_key,
                    audit_enabled=vendor_data.audit_enabled,
                    audit_capture_bodies=vendor_data.audit_capture_bodies,
                )
            )
            created_count += 1
            continue

        if (
            existing.name != vendor_data.name
            or existing.description != vendor_data.description
            or existing.icon_key != vendor_data.icon_key
            or existing.audit_enabled != vendor_data.audit_enabled
            or existing.audit_capture_bodies != vendor_data.audit_capture_bodies
        ):
            existing.name = vendor_data.name
            existing.description = vendor_data.description
            existing.icon_key = vendor_data.icon_key
            existing.audit_enabled = vendor_data.audit_enabled
            existing.audit_capture_bodies = vendor_data.audit_capture_bodies
            updated_count += 1

    return ConfigVendorCatalogImportResponse(
        created_count=created_count,
        updated_count=updated_count,
    )
