import json
import logging
import re
from urllib.parse import urlparse
from typing import AsyncGenerator

import httpx

from app.models.models import Endpoint, HeaderBlocklistRule

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


_DOUBLE_SEGMENT_RE = re.compile(r"(/v\d+)\1")


def normalize_base_url(raw_url: str) -> str:
    """Strip trailing slashes from a base URL for consistent path joining."""
    return raw_url.rstrip("/")


def validate_base_url(base_url: str) -> list[str]:
    """Return a list of warnings about a base_url (empty list = OK)."""
    warnings: list[str] = []
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
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


def build_upstream_url(endpoint: Endpoint, request_path: str) -> str:
    """Forward the exact request path to the endpoint's base URL.

    Handles overlapping path prefixes between base_url and request_path.
    e.g. base_url="https://api.example.com/v1" + request_path="/v1/responses"
         -> "https://api.example.com/v1/responses" (not /v1/v1/responses)
    """
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(endpoint.base_url)
    base_path = parsed.path.rstrip("/")
    req_path = request_path if request_path.startswith("/") else f"/{request_path}"

    if base_path and req_path.startswith(base_path):
        final_path = req_path
    else:
        final_path = f"{base_path}{req_path}"

    if _DOUBLE_SEGMENT_RE.search(final_path):
        fixed_path = _DOUBLE_SEGMENT_RE.sub(r"\1", final_path)
        logger.warning(
            "Double version segment detected in URL path: %s -> auto-corrected to %s "
            "(base_url=%s, request_path=%s)",
            final_path,
            fixed_path,
            endpoint.base_url,
            request_path,
        )
        final_path = fixed_path

    return urlunparse((parsed.scheme, parsed.netloc, final_path, "", "", ""))


def build_upstream_headers(
    endpoint: Endpoint,
    provider_type: str,
    client_headers: dict[str, str] | None = None,
    blocklist_rules: list[HeaderBlocklistRule] | None = None,
) -> dict[str, str]:
    """Build headers for the upstream request.

    Merge order:
    1. Client headers (minus hop-by-hop, minus client auth, minus proxy-controlled)
    2. Provider auth headers
    3. Provider extra headers (e.g., anthropic-version)
    4. Endpoint custom_headers — applied LAST, overwrites same-name
    5. Blocklist sanitization applied twice: on client headers and on final merged result
    """
    auth_key = endpoint.auth_type or provider_type
    config = PROVIDER_AUTH.get(auth_key, PROVIDER_AUTH["openai"])

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

    headers[config["auth_header"]] = f"{config['auth_prefix']}{endpoint.api_key}"
    headers.update(config["extra_headers"])

    if endpoint.custom_headers:
        try:
            custom = json.loads(endpoint.custom_headers)
            if isinstance(custom, dict):
                headers.update(custom)
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

    return headers


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


def inject_stream_options(
    raw_body: bytes | None, provider_type: str
) -> bytes | None:
    """Strip OpenAI SDK stream_options for cross-provider compatibility.

    Some upstreams (including OpenAI-compatible hosts) reject stream_options.
    Prism accepts OpenAI-shaped payloads, so this proxy layer removes the field
    before forwarding regardless of provider type/streaming mode.
    """
    _ = provider_type
    if not raw_body:
        return raw_body

    try:
        parsed = json.loads(raw_body)
        if not isinstance(parsed, dict):
            return raw_body

        if "stream_options" not in parsed:
            return raw_body

        parsed.pop("stream_options", None)
        return json.dumps(parsed, separators=(",", ":")).encode("utf-8")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw_body
