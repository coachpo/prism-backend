from unittest.mock import MagicMock

import pytest

from app.services.proxy_service import (
    build_upstream_headers,
    filter_response_headers,
)


class TestHeaderBlocklist:
    """Header blocklist feature: exact/prefix matching, sanitization, and schema validation."""

    def test_header_is_blocked_exact_match(self):
        """HBL-001 (P0): header_is_blocked() returns True for exact match."""
        from app.services.proxy_service import header_is_blocked

        rule = MagicMock()
        rule.match_type = "exact"
        rule.pattern = "x-custom-header"
        rule.enabled = True

        assert header_is_blocked("X-Custom-Header", [rule]) is True
        assert header_is_blocked("x-custom-header", [rule]) is True

    def test_header_is_blocked_prefix_match(self):
        """HBL-002 (P0): header_is_blocked() returns True for prefix match."""
        from app.services.proxy_service import header_is_blocked

        rule = MagicMock()
        rule.match_type = "prefix"
        rule.pattern = "x-custom-"
        rule.enabled = True

        assert header_is_blocked("X-Custom-Foo", [rule]) is True
        assert header_is_blocked("x-custom-bar", [rule]) is True
        assert header_is_blocked("X-Other", [rule]) is False

    def test_header_is_blocked_returns_false_for_non_matching(self):
        """HBL-003 (P0): header_is_blocked() returns False when no rules match."""
        from app.services.proxy_service import header_is_blocked

        rule = MagicMock()
        rule.match_type = "exact"
        rule.pattern = "x-blocked"
        rule.enabled = True

        assert header_is_blocked("X-Allowed", [rule]) is False

    def test_header_is_blocked_skips_disabled_rules(self):
        """HBL-004 (P0): header_is_blocked() skips disabled rules."""
        from app.services.proxy_service import header_is_blocked

        rule = MagicMock()
        rule.match_type = "exact"
        rule.pattern = "x-blocked"
        rule.enabled = False

        assert header_is_blocked("X-Blocked", [rule]) is False

    def test_sanitize_headers_removes_blocked(self):
        """HBL-005 (P0): sanitize_headers() removes blocked headers."""
        from app.services.proxy_service import sanitize_headers

        rule = MagicMock()
        rule.match_type = "exact"
        rule.pattern = "x-blocked"
        rule.enabled = True

        headers = {"X-Blocked": "value", "X-Allowed": "value"}
        result = sanitize_headers(headers, [rule])

        assert "X-Blocked" not in result
        assert result["X-Allowed"] == "value"

    def test_sanitize_headers_preserves_non_blocked(self):
        """HBL-006 (P0): sanitize_headers() preserves non-blocked headers."""
        from app.services.proxy_service import sanitize_headers

        rule = MagicMock()
        rule.match_type = "prefix"
        rule.pattern = "x-block-"
        rule.enabled = True

        headers = {"X-Block-Foo": "value", "X-Allowed": "value", "Content-Type": "json"}
        result = sanitize_headers(headers, [rule])

        assert "X-Block-Foo" not in result
        assert result["X-Allowed"] == "value"
        assert result["Content-Type"] == "json"

    def test_build_upstream_headers_with_blocklist_strips_client_headers(self):
        """HBL-007 (P0): build_upstream_headers() with blocklist_rules strips blocked client headers."""
        from app.services.proxy_service import build_upstream_headers

        ep = MagicMock()
        ep.auth_type = None
        ep.api_key = "sk-test"
        ep.custom_headers = None

        rule = MagicMock()
        rule.match_type = "exact"
        rule.pattern = "x-blocked"
        rule.enabled = True

        client_headers = {"X-Blocked": "value", "X-Allowed": "value"}
        headers = build_upstream_headers(
            ep, "openai", client_headers=client_headers, blocklist_rules=[rule]
        )

        assert "X-Blocked" not in headers
        assert headers["X-Allowed"] == "value"
        assert headers["Authorization"] == "Bearer sk-test"

    def test_build_upstream_headers_protects_auth_from_blocklist(self):
        """HBL-008 (P0): build_upstream_headers() protects auth headers from blocklist."""
        from app.services.proxy_service import build_upstream_headers

        ep = MagicMock()
        ep.auth_type = None
        ep.api_key = "sk-test"
        ep.custom_headers = None

        rule = MagicMock()
        rule.match_type = "exact"
        rule.pattern = "authorization"
        rule.enabled = True

        headers = build_upstream_headers(ep, "openai", blocklist_rules=[rule])

        assert headers["Authorization"] == "Bearer sk-test"

    def test_build_upstream_headers_drops_accept_encoding_but_keeps_content_encoding(
        self,
    ):
        ep = MagicMock()
        ep.auth_type = None
        ep.api_key = "sk-test"
        ep.custom_headers = None

        headers = build_upstream_headers(
            ep,
            "openai",
            client_headers={
                "Accept-Encoding": "gzip, br",
                "Content-Encoding": "gzip",
                "Content-Length": "42",
                "X-Trace": "trace-1",
            },
        )

        header_names = {key.lower() for key in headers}
        assert "accept-encoding" not in header_names
        assert "content-length" not in header_names
        assert headers["Content-Encoding"] == "gzip"
        assert headers["X-Trace"] == "trace-1"

    def test_build_upstream_headers_skips_invalid_custom_auth_override(self):
        """HBL-008A (P0): invalid custom auth override must not clobber valid auth header."""
        from app.services.proxy_service import build_upstream_headers

        ep = MagicMock()
        ep.auth_type = None
        ep.api_key = "sk-test"
        ep.custom_headers = '{"x-api-key":"\\tbad\\nkey","x-trace":"\\ttrace-1\\t"}'

        headers = build_upstream_headers(ep, "anthropic")

        assert headers["x-api-key"] == "sk-test"
        assert headers["x-trace"] == "trace-1"

    def test_header_blocklist_rule_create_validates_prefix_ends_with_dash(self):
        """HBL-009 (P1): HeaderBlocklistRuleCreate validates prefix pattern must end with '-'."""
        from app.schemas.schemas import HeaderBlocklistRuleCreate
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            HeaderBlocklistRuleCreate(
                name="Test", match_type="prefix", pattern="x-custom", enabled=True
            )
        assert "prefix pattern must end with '-'" in str(exc_info.value)

        valid = HeaderBlocklistRuleCreate(
            name="Test", match_type="prefix", pattern="x-custom-", enabled=True
        )
        assert valid.pattern == "x-custom-"

    def test_header_blocklist_rule_create_rejects_invalid_pattern_chars(self):
        """HBL-010 (P1): HeaderBlocklistRuleCreate rejects invalid pattern characters."""
        from app.schemas.schemas import HeaderBlocklistRuleCreate
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            HeaderBlocklistRuleCreate(
                name="Test", match_type="exact", pattern="X-Custom_Header", enabled=True
            )
        assert "lowercase alphanumeric characters and hyphens" in str(exc_info.value)

        with pytest.raises(ValidationError) as exc_info:
            HeaderBlocklistRuleCreate(
                name="Test", match_type="exact", pattern="x custom", enabled=True
            )
        assert "lowercase alphanumeric characters and hyphens" in str(exc_info.value)

    def test_header_blocklist_rule_export_roundtrip(self):
        """HBL-011 (P1): HeaderBlocklistRuleExport schema roundtrip preserves all fields."""
        from app.schemas.schemas import HeaderBlocklistRuleExport

        rule = HeaderBlocklistRuleExport(
            name="Block Custom",
            match_type="prefix",
            pattern="x-custom-",
            enabled=True,
        )
        exported = rule.model_dump(mode="json")
        reimported = HeaderBlocklistRuleExport(**exported)

        assert reimported.name == "Block Custom"
        assert reimported.match_type == "prefix"
        assert reimported.pattern == "x-custom-"
        assert reimported.enabled is True

    def test_config_export_response_includes_header_blocklist_rules(self):
        """HBL-012 (P1): ConfigExportResponse schema includes header_blocklist_rules field."""
        from app.schemas.schemas import (
            ConfigExportResponse,
            HeaderBlocklistRuleExport,
        )
        from datetime import datetime, timezone

        config = ConfigExportResponse(
            exported_at=datetime.now(timezone.utc),
            endpoints=[],
            models=[],
            pricing_templates=[],
            header_blocklist_rules=[
                HeaderBlocklistRuleExport(
                    name="Block Custom",
                    match_type="prefix",
                    pattern="x-custom-",
                    enabled=True,
                )
            ],
        )

        assert len(config.header_blocklist_rules) == 1
        assert config.header_blocklist_rules[0].pattern == "x-custom-"

    def test_filter_response_headers_removes_stale_compression_metadata(self):
        import httpx

        filtered = filter_response_headers(
            httpx.Headers(
                {
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                    "Content-Length": "128",
                    "Transfer-Encoding": "chunked",
                    "X-Trace": "trace-1",
                }
            )
        )

        header_names = {key.lower() for key in filtered}
        assert "content-encoding" not in header_names
        assert "content-length" not in header_names
        assert "transfer-encoding" not in header_names
        assert filtered["content-type"] == "application/json"
        assert filtered["x-trace"] == "trace-1"
