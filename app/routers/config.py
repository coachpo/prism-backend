from datetime import datetime, timezone
from typing import Annotated
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_db
from app.models.models import Provider, ModelConfig, Endpoint
from app.schemas.schemas import (
    ConfigExportResponse,
    ConfigProviderExport,
    ConfigModelExport,
    ConfigEndpointExport,
    ConfigImportRequest,
    ConfigImportResponse,
)

router = APIRouter(prefix="/api/config", tags=["config"])

VALID_PROVIDER_TYPES = {"openai", "anthropic", "gemini"}


@router.get("/export")
async def export_config(db: Annotated[AsyncSession, Depends(get_db)]):
    providers = (await db.execute(select(Provider))).scalars().all()
    models_q = select(ModelConfig).options(selectinload(ModelConfig.endpoints))
    model_configs = (await db.execute(models_q)).scalars().all()

    provider_type_map = {p.id: p.provider_type for p in providers}

    exported_providers = [
        ConfigProviderExport(
            name=p.name,
            provider_type=p.provider_type,
            description=p.description,
            audit_enabled=p.audit_enabled,
            audit_capture_bodies=p.audit_capture_bodies,
        )
        for p in providers
    ]

    exported_models = [
        ConfigModelExport(
            provider_type=provider_type_map.get(mc.provider_id, ""),
            model_id=mc.model_id,
            display_name=mc.display_name,
            model_type=mc.model_type,
            redirect_to=mc.redirect_to,
            lb_strategy=mc.lb_strategy,
            is_enabled=mc.is_enabled,
            endpoints=[
                ConfigEndpointExport(
                    base_url=ep.base_url,
                    api_key=ep.api_key,
                    is_active=ep.is_active,
                    priority=ep.priority,
                    description=ep.description,
                    auth_type=ep.auth_type,
                    custom_headers=json.loads(ep.custom_headers)
                    if ep.custom_headers
                    else None,
                )
                for ep in mc.endpoints
            ],
        )
        for mc in model_configs
    ]

    data = ConfigExportResponse(
        version=1,
        exported_at=datetime.now(timezone.utc),
        providers=exported_providers,
        models=exported_models,
    )

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return JSONResponse(
        content=data.model_dump(mode="json"),
        headers={
            "Content-Disposition": f'attachment; filename="gateway-config-{date_str}.json"'
        },
    )


def _validate_import(data: ConfigImportRequest) -> None:
    if data.version != 1:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported config version: {data.version}. Expected: 1",
        )

    if not data.providers:
        raise HTTPException(status_code=400, detail="At least one provider is required")

    seen_provider_types: set[str] = set()
    for p in data.providers:
        if p.provider_type not in VALID_PROVIDER_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown provider type: '{p.provider_type}'",
            )
        if p.provider_type in seen_provider_types:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate provider type: '{p.provider_type}'",
            )
        seen_provider_types.add(p.provider_type)

    provider_types_in_file = {p.provider_type for p in data.providers}
    seen_model_ids: set[str] = set()
    native_models: dict[str, str] = {}

    for m in data.models:
        if m.model_id in seen_model_ids:
            raise HTTPException(
                status_code=400, detail=f"Duplicate model_id: '{m.model_id}'"
            )
        seen_model_ids.add(m.model_id)

        if m.provider_type not in provider_types_in_file:
            raise HTTPException(
                status_code=400,
                detail=f"Model '{m.model_id}' references unknown provider type '{m.provider_type}'",
            )

        if m.model_type == "native":
            if m.redirect_to is not None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Native model '{m.model_id}' must not have redirect_to",
                )
            native_models[m.model_id] = m.provider_type
        elif m.model_type == "proxy":
            if m.endpoints:
                raise HTTPException(
                    status_code=400,
                    detail=f"Proxy model '{m.model_id}' must not have endpoints",
                )

    for m in data.models:
        if m.model_type == "proxy":
            if not m.redirect_to or m.redirect_to not in native_models:
                raise HTTPException(
                    status_code=400,
                    detail=f"Model '{m.model_id}' references unknown redirect target '{m.redirect_to}'",
                )
            if native_models[m.redirect_to] != m.provider_type:
                raise HTTPException(
                    status_code=400,
                    detail=f"Model '{m.model_id}' cannot redirect cross-provider to '{m.redirect_to}'",
                )


@router.post("/import", response_model=ConfigImportResponse)
async def import_config(
    data: ConfigImportRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _validate_import(data)

    await db.execute(delete(Endpoint))
    await db.execute(delete(ModelConfig))
    await db.execute(delete(Provider))
    await db.flush()

    provider_map: dict[str, int] = {}
    for p in data.providers:
        provider = Provider(
            name=p.name,
            provider_type=p.provider_type,
            description=p.description,
            audit_enabled=p.audit_enabled,
            audit_capture_bodies=p.audit_capture_bodies,
        )
        db.add(provider)
        await db.flush()
        provider_map[p.provider_type] = provider.id

    endpoints_count = 0

    native_models = [m for m in data.models if m.model_type == "native"]
    proxy_models = [m for m in data.models if m.model_type == "proxy"]

    for m in native_models:
        mc = ModelConfig(
            provider_id=provider_map[m.provider_type],
            model_id=m.model_id,
            display_name=m.display_name,
            model_type=m.model_type,
            redirect_to=m.redirect_to,
            lb_strategy=m.lb_strategy,
            is_enabled=m.is_enabled,
        )
        db.add(mc)
        await db.flush()

        for ep_data in m.endpoints:
            ep = Endpoint(
                model_config_id=mc.id,
                base_url=ep_data.base_url,
                api_key=ep_data.api_key,
                is_active=ep_data.is_active,
                priority=ep_data.priority,
                description=ep_data.description,
                auth_type=ep_data.auth_type,
                custom_headers=json.dumps(ep_data.custom_headers)
                if ep_data.custom_headers
                else None,
            )
            db.add(ep)
            endpoints_count += 1

    for m in proxy_models:
        mc = ModelConfig(
            provider_id=provider_map[m.provider_type],
            model_id=m.model_id,
            display_name=m.display_name,
            model_type=m.model_type,
            redirect_to=m.redirect_to,
            lb_strategy=m.lb_strategy,
            is_enabled=m.is_enabled,
        )
        db.add(mc)

    await db.flush()

    return ConfigImportResponse(
        providers_imported=len(data.providers),
        models_imported=len(data.models),
        endpoints_imported=endpoints_count,
    )
