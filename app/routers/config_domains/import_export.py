from typing import Annotated
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_effective_profile_id
from app.schemas.schemas import ConfigImportRequest, ConfigImportResponse

from .export_builder import build_export_payload
from .import_executor import execute_import_payload
from .import_validator import validate_import_payload

logger = logging.getLogger(__name__)

router = APIRouter()
_validate_import = validate_import_payload


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
    if data.version != 3:
        raise HTTPException(status_code=400, detail="Config import requires version=3")
    return await execute_import_payload(db, profile_id=profile_id, data=data)
