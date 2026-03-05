from datetime import datetime, timedelta

from app.core.time import utc_now


def resolve_time_preset(
    preset: str | None,
    from_time: datetime | None,
    to_time: datetime | None,
 ) -> tuple[datetime | None, datetime | None]:
    if preset in (None, "", "custom"):
        return from_time, to_time

    now = utc_now()
    if preset == "today":
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return today_start, to_time
    if preset == "24h":
        return now - timedelta(days=1), to_time
    if preset in ("last_7_days", "7d"):
        return now - timedelta(days=7), to_time
    if preset in ("last_30_days", "30d"):
        return now - timedelta(days=30), to_time
    if preset == "all":
        return None, to_time
    return from_time, to_time
