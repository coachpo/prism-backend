from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.time import utc_now
from app.models.domains.identity import WebAuthnChallenge

AUTHENTICATION_CHALLENGE_KEY = "authentication"


def get_rp_id() -> str:
    return get_settings().webauthn_rp_id


def get_rp_name() -> str:
    return get_settings().webauthn_rp_name


def get_origin() -> str:
    return get_settings().webauthn_origin


async def store_challenge(
    db: AsyncSession, challenge_key: str, challenge: bytes
) -> None:
    expires_at = utc_now() + timedelta(minutes=2)
    await db.execute(
        delete(WebAuthnChallenge).where(
            WebAuthnChallenge.challenge_key == challenge_key
        )
    )
    db.add(
        WebAuthnChallenge(
            challenge_key=challenge_key,
            challenge=challenge,
            expires_at=expires_at,
        )
    )
    await db.flush()


async def get_challenge(db: AsyncSession, challenge_key: str) -> bytes | None:
    stmt = select(WebAuthnChallenge).where(
        WebAuthnChallenge.challenge_key == challenge_key
    )
    result = await db.execute(stmt)
    db_challenge = result.scalar_one_or_none()

    if db_challenge is None:
        return None

    if utc_now() > db_challenge.expires_at:
        await db.delete(db_challenge)
        await db.flush()
        return None

    return db_challenge.challenge


async def clear_challenge(db: AsyncSession, challenge_key: str) -> None:
    await db.execute(
        delete(WebAuthnChallenge).where(
            WebAuthnChallenge.challenge_key == challenge_key
        )
    )
    await db.flush()


def serialize_aaguid(aaguid: str | None) -> bytes | None:
    if not aaguid:
        return None
    try:
        return UUID(aaguid).bytes
    except ValueError:
        return None


__all__ = [
    "AUTHENTICATION_CHALLENGE_KEY",
    "clear_challenge",
    "get_challenge",
    "get_origin",
    "get_rp_id",
    "get_rp_name",
    "serialize_aaguid",
    "store_challenge",
]
