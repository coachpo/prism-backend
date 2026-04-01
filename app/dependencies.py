import asyncio
import logging
from collections.abc import Callable
from typing import Annotated, AsyncGenerator

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.models import Profile
from app.services.profile_invariants import ensure_profile_invariants

logger = logging.getLogger(__name__)
_AFTER_COMMIT_ACTIONS_KEY = "after_commit_actions"

AfterCommitAction = Callable[[], None]


PROFILE_ID_HEADER = "X-Profile-Id"


async def _rollback_session_quietly(session: AsyncSession, *, reason: str) -> None:
    try:
        await session.rollback()
    except Exception:
        logger.exception("Database session rollback failed during %s", reason)


def register_after_commit_action(
    session: AsyncSession,
    action: AfterCommitAction,
) -> None:
    actions = session.info.setdefault(_AFTER_COMMIT_ACTIONS_KEY, [])
    actions.append(action)


def _clear_after_commit_actions(session: AsyncSession) -> None:
    session.info.pop(_AFTER_COMMIT_ACTIONS_KEY, None)


def _run_after_commit_actions(session: AsyncSession) -> None:
    actions = session.info.pop(_AFTER_COMMIT_ACTIONS_KEY, [])
    for action in actions:
        try:
            action()
        except Exception:
            logger.exception("Post-commit action failed")


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
            _run_after_commit_actions(session)
        except asyncio.CancelledError:
            _clear_after_commit_actions(session)
            await _rollback_session_quietly(session, reason="request cancellation")
            raise
        except Exception:
            _clear_after_commit_actions(session)
            await _rollback_session_quietly(session, reason="request exception")
            raise


async def _get_non_deleted_profile(
    db: AsyncSession,
    *,
    profile_id: int,
) -> Profile | None:
    result = await db.execute(
        select(Profile)
        .where(Profile.id == profile_id, Profile.deleted_at.is_(None))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_active_profile(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Profile:
    profile = await ensure_profile_invariants(db)
    return profile


async def get_effective_profile(
    db: Annotated[AsyncSession, Depends(get_db)],
    x_profile_id: Annotated[str | None, Header(alias=PROFILE_ID_HEADER)] = None,
) -> Profile:
    if x_profile_id is None:
        raise HTTPException(
            status_code=400, detail=f"{PROFILE_ID_HEADER} header is required"
        )
    try:
        profile_id = int(x_profile_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"{PROFILE_ID_HEADER} must be an integer"
        ) from exc

    if profile_id <= 0:
        raise HTTPException(
            status_code=400, detail=f"{PROFILE_ID_HEADER} must be a positive integer"
        )

    profile = await _get_non_deleted_profile(db, profile_id=profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"Profile {profile_id} not found")
    return profile


async def get_active_profile_id(
    profile: Annotated[Profile, Depends(get_active_profile)],
) -> int:
    return profile.id


async def get_effective_profile_id(
    profile: Annotated[Profile, Depends(get_effective_profile)],
) -> int:
    return profile.id


async def get_request_auth_subject(request: Request) -> dict[str, object]:
    auth_subject = getattr(request.state, "auth_subject", None)
    if not isinstance(auth_subject, dict):
        raise HTTPException(status_code=401, detail="Authentication required")
    return auth_subject
