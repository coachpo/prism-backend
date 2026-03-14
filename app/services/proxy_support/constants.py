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

CLIENT_AUTH_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "x-goog-api-key",
    }
)

AUTO_DECOMPRESSED_RESPONSE_HEADERS = frozenset(
    {
        "content-encoding",
        "content-length",
    }
)

__all__ = [
    "AUTO_DECOMPRESSED_RESPONSE_HEADERS",
    "CLIENT_AUTH_HEADERS",
    "FAILOVER_STATUS_CODES",
    "HOP_BY_HOP_HEADERS",
    "PROVIDER_AUTH",
]
