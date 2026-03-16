from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import UserSetting


async def load_profile_user_settings(
    db: AsyncSession,
    *,
    profile_id: int,
) -> UserSetting | None:
    return (
        await db.execute(
            select(UserSetting)
            .where(UserSetting.profile_id == profile_id)
            .order_by(UserSetting.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_report_currency_preferences(
    db: AsyncSession,
    *,
    profile_id: int,
) -> tuple[str, str]:
    settings = await load_profile_user_settings(db, profile_id=profile_id)
    if settings is None:
        return ("USD", "$")
    return (settings.report_currency_code, settings.report_currency_symbol)


__all__ = ["get_report_currency_preferences", "load_profile_user_settings"]
