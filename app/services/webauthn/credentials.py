from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domains.identity import WebAuthnCredential


async def list_credentials_for_user(
    db: AsyncSession,
    auth_subject_id: int,
) -> list[WebAuthnCredential]:
    stmt = (
        select(WebAuthnCredential)
        .where(WebAuthnCredential.auth_subject_id == auth_subject_id)
        .order_by(WebAuthnCredential.created_at.desc())
    )

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def revoke_credential(
    db: AsyncSession,
    credential_id: int,
    auth_subject_id: int,
) -> bool:
    stmt = select(WebAuthnCredential).where(
        WebAuthnCredential.id == credential_id,
        WebAuthnCredential.auth_subject_id == auth_subject_id,
    )
    result = await db.execute(stmt)
    credential = result.scalar_one_or_none()

    if not credential:
        return False

    await db.delete(credential)
    await db.flush()
    return True


__all__ = ["list_credentials_for_user", "revoke_credential"]
