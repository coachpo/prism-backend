from datetime import datetime

from app.core.time import ensure_utc_datetime


def normalize_datetime_filter(value: datetime | None) -> datetime | None:
    return ensure_utc_datetime(value)


def coerce_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def coerce_int(value: object) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    return None


__all__ = ["coerce_float", "coerce_int", "normalize_datetime_filter"]
