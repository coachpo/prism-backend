from typing import Annotated
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import (
    get_db,
    get_effective_profile_id,
    register_after_commit_action,
)
from app.schemas.schemas import ConfigImportRequest, ConfigImportResponse
from app.services.monitoring_service import enqueue_connection_probe

from .export_builder import build_export_payload
from .import_executor import execute_import_payload
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


@router.get("/export")
async def export_config(
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


@router.post("/import", response_model=ConfigImportResponse)
async def import_config(
    data: ConfigImportRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    validate_import_payload(data)
    if data.version != 1:
        raise HTTPException(status_code=400, detail="Config import requires version=1")
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
