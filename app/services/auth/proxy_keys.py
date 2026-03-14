from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import build_proxy_api_key, parse_proxy_api_key
from app.core.crypto import hash_opaque_token, verify_opaque_token
from app.core.time import utc_now
from app.models.models import ProxyApiKey
from app.schemas.schemas import ProxyApiKeyResponse

PROXY_KEY_LIMIT = 10


def _build_key_material() -> tuple[str, str, str, str]:
    raw_key, key_prefix, last_four = build_proxy_api_key()
    return raw_key, key_prefix, last_four, hash_opaque_token(raw_key)


async def list_proxy_api_keys(db: AsyncSession) -> list[ProxyApiKey]:
    result = await db.execute(select(ProxyApiKey).order_by(ProxyApiKey.id.asc()))
    return list(result.scalars().all())


async def create_proxy_api_key(
    db: AsyncSession, *, name: str, notes: str | None, auth_subject_id: int | None
) -> tuple[str, ProxyApiKey]:
    count = await db.execute(select(func.count(ProxyApiKey.id)))
    if count.scalar_one() >= PROXY_KEY_LIMIT:
        raise HTTPException(status_code=409, detail="Maximum 10 proxy API keys reached")

    for _ in range(5):
        raw_key, key_prefix, last_four, key_hash = _build_key_material()
        row = ProxyApiKey(
            name=name,
            key_prefix=key_prefix,
            key_hash=key_hash,
            last_four=last_four,
            is_active=True,
            created_by_auth_subject_id=auth_subject_id,
            notes=notes,
        )
        db.add(row)
        try:
            await db.flush()
            return raw_key, row
        except IntegrityError as exc:
            await db.rollback()
            if "uq_proxy_api_keys_prefix" not in str(exc):
                raise
    raise HTTPException(
        status_code=500, detail="Failed to generate a unique proxy API key"
    )


async def rotate_proxy_api_key(
    db: AsyncSession, *, key_id: int
) -> tuple[str, ProxyApiKey]:
    row = (
        await db.execute(select(ProxyApiKey).where(ProxyApiKey.id == key_id).limit(1))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Proxy API key not found")

    for _ in range(5):
        raw_key, key_prefix, last_four, key_hash = _build_key_material()
        row.key_prefix = key_prefix
        row.key_hash = key_hash
        row.last_four = last_four
        row.updated_at = utc_now()
        try:
            await db.flush()
            return raw_key, row
        except IntegrityError as exc:
            await db.rollback()
            refreshed = (
                await db.execute(
                    select(ProxyApiKey).where(ProxyApiKey.id == key_id).limit(1)
                )
            ).scalar_one_or_none()
            if refreshed is None:
                raise HTTPException(status_code=404, detail="Proxy API key not found")
            row = refreshed
            if "uq_proxy_api_keys_prefix" not in str(exc):
                raise
    raise HTTPException(status_code=500, detail="Failed to rotate proxy API key")


async def delete_proxy_api_key(db: AsyncSession, *, key_id: int) -> None:
    row = (
        await db.execute(select(ProxyApiKey).where(ProxyApiKey.id == key_id).limit(1))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Proxy API key not found")
    await db.delete(row)
    await db.flush()


async def verify_proxy_api_key(db: AsyncSession, *, raw_key: str) -> ProxyApiKey | None:
    try:
        normalized_key, key_prefix = parse_proxy_api_key(raw_key)
    except ValueError:
        return None
    row = (
        await db.execute(
            select(ProxyApiKey)
            .where(ProxyApiKey.key_prefix == key_prefix)
            .order_by(ProxyApiKey.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None or not row.is_active:
        return None
    if row.expires_at is not None and row.expires_at < utc_now():
        return None
    if not verify_opaque_token(normalized_key, row.key_hash):
        return None
    row.last_used_at = utc_now()
    return row


def serialize_proxy_api_key(row: ProxyApiKey) -> ProxyApiKeyResponse:
    return ProxyApiKeyResponse(
        id=row.id,
        name=row.name,
        key_prefix=row.key_prefix,
        key_preview=f"{row.key_prefix}{'\N{BULLET}' * 8}{row.last_four}",
        is_active=row.is_active,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        last_used_ip=row.last_used_ip,
        notes=row.notes,
        rotated_from_id=row.rotated_from_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


__all__ = [
    "PROXY_KEY_LIMIT",
    "create_proxy_api_key",
    "delete_proxy_api_key",
    "list_proxy_api_keys",
    "rotate_proxy_api_key",
    "serialize_proxy_api_key",
    "verify_proxy_api_key",
]
