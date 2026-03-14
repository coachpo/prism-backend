import httpx

from app.models.models import Connection, Endpoint


def normalize_base_url(raw_url: str) -> str:
    return raw_url.rstrip("/")


def validate_base_url(base_url: str) -> list[str]:
    warnings: list[str] = []
    try:
        parsed = httpx.URL(base_url)
    except Exception:
        warnings.append(
            "base_url must include scheme and host (e.g. https://api.example.com/v1)"
        )
        return warnings

    if not parsed.scheme or not parsed.host:
        warnings.append(
            "base_url must include scheme and host (e.g. https://api.example.com/v1)"
        )
    return warnings


def build_upstream_url(
    connection: Connection | Endpoint,
    request_path: str,
    endpoint: Endpoint | None = None,
) -> str:
    endpoint_obj = endpoint or connection
    parsed = httpx.URL(str(endpoint_obj.base_url or ""))
    base_path = parsed.path.rstrip("/")
    req_path = request_path if request_path.startswith("/") else f"/{request_path}"
    final_path = f"{base_path}{req_path}"

    return str(parsed.copy_with(path=final_path))


__all__ = ["build_upstream_url", "normalize_base_url", "validate_base_url"]
