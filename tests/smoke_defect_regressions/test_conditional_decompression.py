"""
DEF-067: Conditional decompression based on audit settings.

When body auditing is disabled, the proxy should request uncompressed responses
(Accept-Encoding: identity) and preserve content-length on identity/no-encoding
responses. If upstream still sends compressed encoding, stale compression
metadata must still be stripped.
"""

import httpx

from app.services.proxy_service import (
    should_request_compressed_response,
    build_upstream_headers,
    filter_response_headers,
)


class TestDEF067_ConditionalDecompression:
    """DEF-067: Conditional decompression based on audit configuration."""

    def test_should_request_compressed_when_audit_enabled_and_bodies_captured(self):
        """DEF-067-001: should_request_compressed_response returns True when both audit flags are True."""
        result = should_request_compressed_response(
            audit_enabled=True, audit_capture_bodies=True
        )
        assert result is True

    def test_should_not_request_compressed_when_audit_disabled(self):
        """DEF-067-002: should_request_compressed_response returns False when audit is disabled."""
        result = should_request_compressed_response(
            audit_enabled=False, audit_capture_bodies=True
        )
        assert result is False

    def test_should_not_request_compressed_when_bodies_not_captured(self):
        """DEF-067-003: should_request_compressed_response returns False when bodies are not captured."""
        result = should_request_compressed_response(
            audit_enabled=True, audit_capture_bodies=False
        )
        assert result is False

    def test_should_not_request_compressed_when_both_disabled(self):
        """DEF-067-004: should_request_compressed_response returns False when both flags are False."""
        result = should_request_compressed_response(
            audit_enabled=False, audit_capture_bodies=False
        )
        assert result is False

    def test_build_upstream_headers_includes_accept_encoding_identity_when_not_compressed(
        self,
    ):
        """DEF-067-005: build_upstream_headers includes Accept-Encoding: identity when request_compressed=False."""
        from unittest.mock import MagicMock

        connection = MagicMock()
        connection.auth_type = None
        connection.api_key = "test-key"
        connection.custom_headers = None

        headers = build_upstream_headers(
            connection=connection,
            provider_type="openai",
            client_headers=None,
            blocklist_rules=None,
            endpoint=None,
            request_compressed=False,
        )

        assert "Accept-Encoding" in headers
        assert headers["Accept-Encoding"] == "identity"

    def test_build_upstream_headers_omits_accept_encoding_when_compressed(self):
        """DEF-067-006: build_upstream_headers omits Accept-Encoding when request_compressed=True (default)."""
        from unittest.mock import MagicMock

        connection = MagicMock()
        connection.auth_type = None
        connection.api_key = "test-key"
        connection.custom_headers = None

        headers = build_upstream_headers(
            connection=connection,
            provider_type="openai",
            client_headers=None,
            blocklist_rules=None,
            endpoint=None,
            request_compressed=True,
        )

        # Should not have Accept-Encoding (let httpx use default)
        assert "Accept-Encoding" not in headers

    def test_filter_response_headers_strips_compression_when_requested_compressed(self):
        """DEF-067-007: filter_response_headers strips compression headers when was_requested_compressed=True."""
        raw_headers = httpx.Headers(
            {
                "content-type": "application/json",
                "content-encoding": "gzip",
                "content-length": "1234",
                "x-custom": "value",
            }
        )

        filtered = filter_response_headers(raw_headers, was_requested_compressed=True)

        assert "content-type" in filtered
        assert "x-custom" in filtered
        assert "content-encoding" not in filtered
        assert "content-length" not in filtered

    def test_filter_response_headers_preserves_content_length_when_identity_path(
        self,
    ):
        """DEF-067-008: preserve content-length when response has no compression header."""
        raw_headers = httpx.Headers(
            {
                "content-type": "application/json",
                "content-length": "1234",
                "x-custom": "value",
            }
        )

        filtered = filter_response_headers(raw_headers, was_requested_compressed=False)

        assert "content-type" in filtered
        assert "x-custom" in filtered
        assert "content-encoding" not in filtered
        assert "content-length" in filtered
        assert filtered["content-length"] == "1234"

    def test_filter_response_headers_strips_stale_metadata_if_upstream_encoded_anyway(
        self,
    ):
        """DEF-067-009: strip stale encoding metadata even on identity-request path."""
        raw_headers = httpx.Headers(
            {
                "content-type": "application/json",
                "content-encoding": "gzip",
                "content-length": "1234",
                "x-custom": "value",
            }
        )

        filtered = filter_response_headers(raw_headers, was_requested_compressed=False)

        assert "content-type" in filtered
        assert "x-custom" in filtered
        assert "content-encoding" not in filtered
        assert "content-length" not in filtered

    def test_filter_response_headers_always_strips_hop_by_hop(self):
        """DEF-067-010: always strip hop-by-hop headers regardless of compression mode."""
        raw_headers = httpx.Headers(
            {
                "content-type": "application/json",
                "connection": "keep-alive",
                "transfer-encoding": "chunked",
                "content-encoding": "gzip",
            }
        )

        # Test with compression requested
        filtered_compressed = filter_response_headers(
            raw_headers, was_requested_compressed=True
        )
        assert "connection" not in filtered_compressed
        assert "transfer-encoding" not in filtered_compressed

        # Test without compression requested
        filtered_uncompressed = filter_response_headers(
            raw_headers, was_requested_compressed=False
        )
        assert "connection" not in filtered_uncompressed
        assert "transfer-encoding" not in filtered_uncompressed
