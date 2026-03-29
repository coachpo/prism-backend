from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_effective_profile_id
from app.schemas.schemas import (
    MonitoringManualProbeResponse,
    MonitoringModelResponse,
    MonitoringOverviewResponse,
    MonitoringVendorResponse,
)
from app.services.monitoring_service import (
    query_monitoring_model,
    query_monitoring_overview,
    query_monitoring_vendor,
    run_connection_probe,
)

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])


@router.get("/overview", response_model=MonitoringOverviewResponse)
async def get_monitoring_overview(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await query_monitoring_overview(db=db, profile_id=profile_id)


@router.get("/vendors/{vendor_id}", response_model=MonitoringVendorResponse)
async def get_monitoring_vendor(
    vendor_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await query_monitoring_vendor(
        db=db,
        profile_id=profile_id,
        vendor_id=vendor_id,
    )


@router.get("/models/{model_config_id}", response_model=MonitoringModelResponse)
async def get_monitoring_model(
    model_config_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    return await query_monitoring_model(
        db=db,
        profile_id=profile_id,
        model_config_id=model_config_id,
    )


@router.post(
    "/connections/{connection_id}/probe",
    response_model=MonitoringManualProbeResponse,
)
async def probe_monitoring_connection(
    connection_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    result = await run_connection_probe(
        db=db,
        client=request.app.state.http_client,
        profile_id=profile_id,
        connection_id=connection_id,
    )
    return MonitoringManualProbeResponse(
        connection_id=result.connection_id,
        checked_at=result.checked_at,
        endpoint_ping_status=result.endpoint_ping_status,
        endpoint_ping_ms=result.endpoint_ping_ms,
        conversation_status=result.conversation_status,
        conversation_delay_ms=result.conversation_delay_ms,
        fused_status=result.fused_status,
        failure_kind=result.failure_kind,
        detail=result.detail,
    )


__all__ = [
    "get_monitoring_model",
    "get_monitoring_overview",
    "get_monitoring_vendor",
    "probe_monitoring_connection",
    "router",
]
