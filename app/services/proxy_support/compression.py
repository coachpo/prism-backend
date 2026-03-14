import httpx

from .constants import AUTO_DECOMPRESSED_RESPONSE_HEADERS, HOP_BY_HOP_HEADERS


def should_request_compressed_response(
    audit_enabled: bool, audit_capture_bodies: bool
) -> bool:
    return audit_enabled and audit_capture_bodies


def filter_response_headers(
    response_headers: httpx.Headers, was_requested_compressed: bool = True
) -> dict[str, str]:
    content_encoding = (response_headers.get("content-encoding") or "").strip().lower()
    upstream_signaled_compression = bool(
        content_encoding and content_encoding != "identity"
    )
    strip_decompression_sensitive_headers = (
        was_requested_compressed or upstream_signaled_compression
    )

    filtered: dict[str, str] = {}
    for key, value in response_headers.items():
        key_lower = key.lower()
        if key_lower in HOP_BY_HOP_HEADERS:
            continue
        if (
            strip_decompression_sensitive_headers
            and key_lower in AUTO_DECOMPRESSED_RESPONSE_HEADERS
        ):
            continue
        filtered[key] = value
    return filtered


__all__ = ["filter_response_headers", "should_request_compressed_response"]
