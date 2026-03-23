from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.schemas import (
    ProxyApiKeyCreate,
    ProxyApiKeyCreateResponse,
    ProxyApiKeyResponse,
    ProxyApiKeyRotateResponse,
    ProxyApiKeyUpdate,
)
from app.services.auth_service import (
    create_proxy_api_key,
    delete_proxy_api_key,
    list_proxy_api_keys,
    rotate_proxy_api_key,
    serialize_proxy_api_key,
    update_proxy_api_key,
)

from .helpers import extract_request_auth_subject_id

router = APIRouter()


@router.get("/auth/proxy-keys", response_model=list[ProxyApiKeyResponse])
async def get_proxy_api_keys(
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return [serialize_proxy_api_key(row) for row in await list_proxy_api_keys(db)]


@router.post(
    "/auth/proxy-keys", response_model=ProxyApiKeyCreateResponse, status_code=201
)
async def post_proxy_api_key(
    body: ProxyApiKeyCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    raw_key, row = await create_proxy_api_key(
        db,
        name=body.name,
        notes=body.notes,
        auth_subject_id=extract_request_auth_subject_id(request),
    )
    return ProxyApiKeyCreateResponse(key=raw_key, item=serialize_proxy_api_key(row))


@router.post(
    "/auth/proxy-keys/{key_id}/rotate", response_model=ProxyApiKeyRotateResponse
)
async def post_rotate_proxy_api_key(
    key_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    raw_key, row = await rotate_proxy_api_key(db, key_id=key_id)
    return ProxyApiKeyRotateResponse(key=raw_key, item=serialize_proxy_api_key(row))


@router.patch("/auth/proxy-keys/{key_id}", response_model=ProxyApiKeyResponse)
async def patch_proxy_api_key(
    key_id: int,
    body: ProxyApiKeyUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await update_proxy_api_key(
        db,
        key_id=key_id,
        name=body.name,
        notes=body.notes,
        is_active=body.is_active,
    )
    return serialize_proxy_api_key(row)


@router.delete("/auth/proxy-keys/{key_id}")
async def remove_proxy_api_key(
    key_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await delete_proxy_api_key(db, key_id=key_id)
    return {"deleted": True}


__all__ = [
    "get_proxy_api_keys",
    "post_proxy_api_key",
    "post_rotate_proxy_api_key",
    "patch_proxy_api_key",
    "remove_proxy_api_key",
    "router",
]
