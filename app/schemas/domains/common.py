import re
from decimal import Decimal, InvalidOperation
from typing import Literal

_HEADER_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")
_CURRENCY_CODE_RE = re.compile(r"^[A-Z]{3}$")

ApiFamily = Literal["openai", "anthropic", "gemini"]
AuthType = ApiFamily


def _validate_decimal_non_negative(value: str | None, field_name: str) -> str | None:
    if value is None or value == "":
        return value
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid decimal") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return f"{parsed}"


__all__ = [
    "AuthType",
    "ApiFamily",
    "_CURRENCY_CODE_RE",
    "_HEADER_TOKEN_RE",
    "_validate_decimal_non_negative",
]
