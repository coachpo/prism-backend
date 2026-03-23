from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from threading import Lock

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import PROXY_API_KEY_PREFIX, build_proxy_api_key, parse_proxy_api_key
from app.core.crypto import hash_opaque_token, verify_opaque_token
from app.core.database import AsyncSessionLocal
from app.core.time import utc_now
from app.models.models import ProxyApiKey
from app.schemas.schemas import ProxyApiKeyResponse
from app.services.background_tasks import BackgroundTaskManager

PROXY_KEY_LIMIT = 100
PROXY_KEY_PREVIEW_LOOKUP_LENGTH = 4
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProxyApiKeyUsageSnapshot:
    key_id: int
    last_used_at: datetime
    last_used_ip: str | None


_proxy_api_key_usage_lock = Lock()
_pending_proxy_api_key_usage: dict[int, ProxyApiKeyUsageSnapshot] = {}
_enqueued_proxy_api_key_ids: set[int] = set()


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
        raise HTTPException(
            status_code=409,
            detail=f"Maximum {PROXY_KEY_LIMIT} proxy API keys reached",
        )

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


async def update_proxy_api_key(
    db: AsyncSession,
    *,
    key_id: int,
    name: str,
    notes: str | None,
    is_active: bool | None = None,
) -> ProxyApiKey:
    row = (
        await db.execute(select(ProxyApiKey).where(ProxyApiKey.id == key_id).limit(1))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Proxy API key not found")

    row.name = name
    row.notes = notes
    if is_active is not None:
        row.is_active = is_active
    row.updated_at = utc_now()
    await db.flush()
    return row


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
    return row


async def record_proxy_api_key_usage(
    db: AsyncSession,
    *,
    key_id: int,
    last_used_at: datetime,
    last_used_ip: str | None,
) -> None:
    row = await db.get(ProxyApiKey, key_id)
    if row is None:
        return

    row.last_used_at = last_used_at
    row.last_used_ip = last_used_ip
    await db.flush()


async def _commit_proxy_api_key_usage_snapshot(
    snapshot: ProxyApiKeyUsageSnapshot,
) -> None:
    async with AsyncSessionLocal() as session:
        try:
            await record_proxy_api_key_usage(
                session,
                key_id=snapshot.key_id,
                last_used_at=snapshot.last_used_at,
                last_used_ip=snapshot.last_used_ip,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def clear_proxy_api_key_usage_write_buffer() -> None:
    with _proxy_api_key_usage_lock:
        _pending_proxy_api_key_usage.clear()
        _enqueued_proxy_api_key_ids.clear()


def enqueue_proxy_api_key_usage(
    background_task_manager: BackgroundTaskManager | None,
    *,
    key_id: int,
    last_used_at: datetime,
    last_used_ip: str | None,
) -> bool:
    if background_task_manager is None or not background_task_manager.started:
        return False

    snapshot = ProxyApiKeyUsageSnapshot(
        key_id=key_id,
        last_used_at=last_used_at,
        last_used_ip=last_used_ip,
    )

    with _proxy_api_key_usage_lock:
        _pending_proxy_api_key_usage[key_id] = snapshot
        if key_id in _enqueued_proxy_api_key_ids:
            return True
        _enqueued_proxy_api_key_ids.add(key_id)

    try:
        background_task_manager.enqueue(
            name=f"proxy_api_key_usage:{key_id}",
            run=lambda key_id=key_id: flush_enqueued_proxy_api_key_usage(key_id=key_id),
            max_retries=1,
            retry_delay_seconds=0.05,
        )
        return True
    except Exception:
        with _proxy_api_key_usage_lock:
            if _pending_proxy_api_key_usage.get(key_id) is snapshot:
                _pending_proxy_api_key_usage.pop(key_id, None)
            _enqueued_proxy_api_key_ids.discard(key_id)
        logger.exception("Failed to enqueue proxy API key usage for key_id=%d", key_id)
        return False


async def flush_enqueued_proxy_api_key_usage(*, key_id: int) -> None:
    while True:
        with _proxy_api_key_usage_lock:
            snapshot = _pending_proxy_api_key_usage.pop(key_id, None)

        if snapshot is None:
            with _proxy_api_key_usage_lock:
                _enqueued_proxy_api_key_ids.discard(key_id)
            return

        try:
            await _commit_proxy_api_key_usage_snapshot(snapshot)
        except Exception:
            with _proxy_api_key_usage_lock:
                _pending_proxy_api_key_usage.setdefault(key_id, snapshot)
            raise

        with _proxy_api_key_usage_lock:
            if key_id not in _pending_proxy_api_key_usage:
                _enqueued_proxy_api_key_ids.discard(key_id)
                return


async def persist_proxy_api_key_usage(
    *,
    key_id: int,
    last_used_at: datetime,
    last_used_ip: str | None,
) -> None:
    snapshot = ProxyApiKeyUsageSnapshot(
        key_id=key_id,
        last_used_at=last_used_at,
        last_used_ip=last_used_ip,
    )
    try:
        await _commit_proxy_api_key_usage_snapshot(snapshot)
    except Exception:
        logger.exception("Failed to persist proxy API key usage for key_id=%d", key_id)


def serialize_proxy_api_key(row: ProxyApiKey) -> ProxyApiKeyResponse:
    visible_prefix = row.key_prefix
    preview_prefix_length = len(PROXY_API_KEY_PREFIX) + PROXY_KEY_PREVIEW_LOOKUP_LENGTH
    if row.key_prefix.startswith(PROXY_API_KEY_PREFIX):
        visible_prefix = row.key_prefix[:preview_prefix_length]

    return ProxyApiKeyResponse(
        id=row.id,
        name=row.name,
        key_prefix=row.key_prefix,
        key_preview=f"{visible_prefix}{'\N{BULLET}' * 8}{row.last_four}",
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
    "ProxyApiKeyUsageSnapshot",
    "clear_proxy_api_key_usage_write_buffer",
    "create_proxy_api_key",
    "delete_proxy_api_key",
    "enqueue_proxy_api_key_usage",
    "flush_enqueued_proxy_api_key_usage",
    "list_proxy_api_keys",
    "persist_proxy_api_key_usage",
    "record_proxy_api_key_usage",
    "rotate_proxy_api_key",
    "serialize_proxy_api_key",
    "update_proxy_api_key",
    "verify_proxy_api_key",
]
