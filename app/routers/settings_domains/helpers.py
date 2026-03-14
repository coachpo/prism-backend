from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import UserSetting


async def get_or_create_user_settings(
    db: AsyncSession,
    *,
    profile_id: int,
) -> UserSetting:
    settings_row = (
        await db.execute(
            select(UserSetting)
            .where(UserSetting.profile_id == profile_id)
            .order_by(UserSetting.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if settings_row is None:
        settings_row = UserSetting(
            profile_id=profile_id,
            report_currency_code="USD",
            report_currency_symbol="$",
            timezone_preference=None,
        )
        db.add(settings_row)
        await db.flush()
    return settings_row


def extract_request_auth_subject_id(request: Request) -> int | None:
    auth_subject = getattr(request.state, "auth_subject", None)
    if not isinstance(auth_subject, dict):
        return None

    auth_subject_value = auth_subject.get("id")
    if auth_subject_value is None:
        return None

    return int(str(auth_subject_value))


__all__ = ["extract_request_auth_subject_id", "get_or_create_user_settings"]
