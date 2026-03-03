from datetime import datetime, timezone


UTC = timezone.utc


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def ensure_utc_datetime(value: datetime | None) -> datetime | None:
    """
    Normalize datetimes to timezone-aware UTC.

    Naive datetimes are treated as already being in UTC.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
