import json
import logging
from typing import AsyncGenerator

import httpx

from app.models.models import Endpoint

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

FAILOVER_STATUS_CODES = {429, 500, 502, 503, 529}

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

    return urlunparse((parsed.scheme, parsed.netloc, final_path, "", "", ""))


def build_upstream_headers(
    endpoint: Endpoint,
    provider_type: str,
    client_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build headers for the upstream request.

    Starts with forwarded client headers (minus hop-by-hop),
    then layers on auth and provider-specific headers which take precedence.
    """
    config = PROVIDER_AUTH.get(provider_type, PROVIDER_AUTH["openai"])

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
                and k_lower != "content-length"
                and k_lower != "accept-encoding"
                and k_lower not in proxy_controlled_headers
            ):
                headers[key] = value

    headers[config["auth_header"]] = f"{config['auth_prefix']}{endpoint.api_key}"
    headers.update(config["extra_headers"])

    return headers


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
    return await client.request(method, upstream_url, **kwargs)


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


def extract_stream_flag(raw_body: bytes) -> bool:
    try:
        parsed = json.loads(raw_body)
        return bool(parsed.get("stream", False))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
