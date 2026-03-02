import json
import logging
import re
from typing import AsyncGenerator

import httpx

from app.models.models import Connection, Endpoint, HeaderBlocklistRule

logger = logging.getLogger(__name__)

PROVIDER_AUTH = {
    "openai": {
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "extra_headers": {},
    },
    "anthropic": {
        "auth_header": "x-api-key",
        "auth_prefix": "",
        "extra_headers": {
            "anthropic-version": "2023-06-01",
        },
    },
    "gemini": {
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "extra_headers": {},
    },
}

FAILOVER_STATUS_CODES = {403, 429, 500, 502, 503, 529}
_DOUBLE_SEGMENT_RE = re.compile(r"/(v\d+)/(?:\1)(?:/|$)")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1F\x7F]")
# Hop-by-hop headers that MUST NOT be forwarded (RFC 2616 §13.5.1)
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
    }
)

# Auth headers from clients must never be forwarded — the proxy replaces auth entirely
CLIENT_AUTH_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "x-goog-api-key",
    }
)


def normalize_base_url(raw_url: str) -> str:
    """Strip trailing slashes from a base URL for consistent path joining."""
    return raw_url.rstrip("/")


def validate_base_url(base_url: str) -> list[str]:
    """Return a list of warnings about a base_url (empty list = OK)."""
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
    path = parsed.path.rstrip("/")
    if _DOUBLE_SEGMENT_RE.search(path):
        warnings.append(
            f"base_url path '{path}' contains a repeated version segment (e.g. /v1/v1). "
            "This is likely a misconfiguration."
        )
    return warnings


def build_upstream_url(
    connection: Connection | Endpoint,
    request_path: str,
    endpoint: Endpoint | None = None,
) -> str:
    """Forward the exact request path to the endpoint's base URL.

    Handles overlapping path prefixes between base_url and request_path.
    e.g. base_url="https://api.example.com/v1" + request_path="/v1/responses"
         -> "https://api.example.com/v1/responses" (not /v1/v1/responses)
    """
    endpoint_obj = endpoint or connection
    parsed = httpx.URL(str(endpoint_obj.base_url or ""))
    base_path = parsed.path.rstrip("/")
    req_path = request_path if request_path.startswith("/") else f"/{request_path}"

    if base_path and req_path.startswith(base_path):
        final_path = req_path
    else:
        final_path = f"{base_path}{req_path}"

    return str(parsed.copy_with(path=final_path))


def _normalize_header_value(value: object | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if _CONTROL_CHAR_RE.search(normalized):
        return None
    return normalized


def _normalize_header_values(headers: dict[str, object]) -> dict[str, str]:
    normalized_headers: dict[str, str] = {}
    for key, raw_value in headers.items():
        normalized_value = _normalize_header_value(raw_value)
        if normalized_value is None:
            logger.warning("Dropping header '%s' due to invalid value", key)
            continue
        normalized_headers[key] = normalized_value
    return normalized_headers


def build_upstream_headers(
    connection: Connection | Endpoint,
    provider_type: str,
    client_headers: dict[str, str] | None = None,
    blocklist_rules: list[HeaderBlocklistRule] | None = None,
    endpoint: Endpoint | None = None,
) -> dict[str, str]:
    """Build headers for the upstream request.

    Merge order:
    1. Client headers (minus hop-by-hop, minus client auth, minus proxy-controlled)
    2. Provider auth headers
    3. Provider extra headers (e.g., anthropic-version)
    4. Endpoint custom_headers — applied LAST, overwrites same-name
    5. Blocklist sanitization applied twice: on client headers and on final merged result
    """
    auth_key = getattr(connection, "auth_type", None) or provider_type
    config = PROVIDER_AUTH.get(auth_key)
    if config is None:
        raise ValueError(f"Unsupported auth_type: {auth_key}")
    endpoint_obj = endpoint or connection
    api_key = getattr(endpoint_obj, "api_key", None)
    custom_headers = getattr(connection, "custom_headers", None)

    proxy_controlled_headers = {
        config["auth_header"].lower(),
        *(k.lower() for k in config["extra_headers"]),
    }

    headers: dict[str, str] = {}

    if client_headers:
        for key, value in client_headers.items():
            k_lower = key.lower()
            if (
                k_lower not in HOP_BY_HOP_HEADERS
                and k_lower not in CLIENT_AUTH_HEADERS
                and k_lower != "content-length"
                and k_lower != "accept-encoding"
                and k_lower not in proxy_controlled_headers
            ):
                headers[key] = value

    if blocklist_rules:
        headers = sanitize_headers(headers, blocklist_rules)

    normalized_api_key = _normalize_header_value(api_key) or ""
    headers[config["auth_header"]] = f"{config['auth_prefix']}{normalized_api_key}"
    headers.update(config["extra_headers"])

    if custom_headers:
        try:
            custom = json.loads(custom_headers)
            if isinstance(custom, dict):
                for key, raw_value in custom.items():
                    normalized_custom_value = _normalize_header_value(raw_value)
                    if normalized_custom_value is None:
                        logger.warning(
                            "Skipping custom header '%s' due to invalid value", key
                        )
                        continue
                    headers[key] = normalized_custom_value
        except (json.JSONDecodeError, TypeError):
            pass

    if blocklist_rules:
        auth_header_lower = config["auth_header"].lower()
        extra_lower = {k.lower() for k in config["extra_headers"]}
        protected = {auth_header_lower} | extra_lower

        sanitized = {}
        for key, value in headers.items():
            if key.lower() in protected:
                sanitized[key] = value
            elif not header_is_blocked(key, blocklist_rules):
                sanitized[key] = value
            else:
                logger.debug("Blocked header (post-merge): %s", key)
        headers = sanitized

    return _normalize_header_values(headers)


def header_is_blocked(name: str, rules: list[HeaderBlocklistRule]) -> bool:
    name_lower = name.lower()
    for rule in rules:
        if not rule.enabled:
            continue
        if rule.match_type == "exact" and name_lower == rule.pattern:
            return True
        if rule.match_type == "prefix" and name_lower.startswith(rule.pattern):
            return True
    return False


def sanitize_headers(
    headers: dict[str, str], rules: list[HeaderBlocklistRule]
) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        if header_is_blocked(key, rules):
            logger.debug("Blocked header: %s", key)
        else:
            result[key] = value
    return result


def filter_response_headers(response_headers: httpx.Headers) -> dict[str, str]:
    """Filter upstream response headers, removing hop-by-hop headers."""
    filtered: dict[str, str] = {}
    for key, value in response_headers.items():
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "content-length":
            filtered[key] = value
    return filtered


async def proxy_request(
    client: httpx.AsyncClient,
    method: str,
    upstream_url: str,
    headers: dict[str, str],
    raw_body: bytes | None,
) -> httpx.Response:
    """Send a non-streaming request to the upstream provider."""
    kwargs: dict = {"headers": headers}
    if raw_body:
        kwargs["content"] = raw_body
    send_req = client.build_request(method, upstream_url, **kwargs)
    return await client.send(send_req, follow_redirects=True)


async def proxy_stream(
    client: httpx.AsyncClient,
    method: str,
    upstream_url: str,
    headers: dict[str, str],
    raw_body: bytes | None,
) -> AsyncGenerator[tuple[bytes, httpx.Headers, int], None]:
    """Stream a response from the upstream provider.

    Yields (chunk, response_headers, status_code).
    For error responses, reads the full body and yields it as a single chunk
    so the caller can forward it transparently.
    """
    kwargs: dict = {"headers": headers}
    if raw_body:
        kwargs["content"] = raw_body
    async with client.stream(method, upstream_url, **kwargs) as response:
        if response.status_code >= 400:
            await response.aread()
            yield response.content, response.headers, response.status_code
            return
        async for chunk in response.aiter_bytes():
            if chunk:
                yield chunk, response.headers, response.status_code


def should_failover(status_code: int) -> bool:
    return status_code in FAILOVER_STATUS_CODES


def extract_model_from_body(raw_body: bytes) -> str | None:
    """Extract the model ID from the raw request body bytes.

    Parses JSON minimally just to read the 'model' key.
    """
    try:
        parsed = json.loads(raw_body)
        return parsed.get("model")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def rewrite_model_in_body(raw_body: bytes | None, target_model_id: str) -> bytes | None:
    if not raw_body:
        return raw_body
    try:
        parsed = json.loads(raw_body)
        parsed["model"] = target_model_id
        return json.dumps(parsed, separators=(",", ":")).encode("utf-8")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw_body


def extract_stream_flag(raw_body: bytes) -> bool:
    try:
        parsed = json.loads(raw_body)
        return bool(parsed.get("stream", False))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
