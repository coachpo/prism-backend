from app.core.time import utc_now


def normalize_ordered_field(items: list[object], *, field_name: str) -> None:
    now = utc_now()
    for index, item in enumerate(items):
        if getattr(item, field_name) == index:
            continue
        setattr(item, field_name, index)
        item.updated_at = now


__all__ = ["normalize_ordered_field"]
