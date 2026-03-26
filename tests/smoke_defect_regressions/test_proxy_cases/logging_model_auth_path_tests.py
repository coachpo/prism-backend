import asyncio
import json
import logging
from typing import AsyncGenerator, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.proxy_service import (
    extract_model_from_body,
    build_upstream_headers,
)
from app.services.stats_service import log_request


class TestDEF001_LogsSurviveFailoverRollback:
    """DEF-001 (P0): request_logs must persist even when HTTPException(502) is raised."""

    @pytest.mark.asyncio
    async def test_log_request_uses_independent_session(self):
        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "app.core.database.AsyncSessionLocal",
            return_value=mock_session_ctx,
        ):
            await log_request(
                model_id="test-model",
                profile_id=1,
                provider_type="openai",
                endpoint_id=1,
                connection_id=1,
                endpoint_base_url="http://example.com",
                status_code=503,
                response_time_ms=100,
                is_stream=False,
                request_path="/v1/chat/completions",
                error_detail="upstream failed",
            )

        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_log_request_uses_independent_session_without_caller_db(self):
        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "app.core.database.AsyncSessionLocal",
            return_value=mock_session_ctx,
        ):
            await log_request(
                model_id="test-model",
                profile_id=1,
                provider_type="openai",
                endpoint_id=1,
                connection_id=1,
                endpoint_base_url="http://example.com",
                status_code=0,
                response_time_ms=50,
                is_stream=False,
                request_path="/v1/chat/completions",
                error_detail="connection refused",
            )
        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_log_request_persists_request_tracking_fields(self):
        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        captured_entry = {}

        async def fake_refresh(entry):
            entry.id = 41
            captured_entry["entry"] = entry

        mock_session.refresh = AsyncMock(side_effect=fake_refresh)

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "app.core.database.AsyncSessionLocal",
            return_value=mock_session_ctx,
        ):
            request_log_id = await log_request(
                model_id="test-model",
                profile_id=1,
                provider_type="openai",
                endpoint_id=1,
                connection_id=1,
                endpoint_base_url="http://example.com",
                status_code=503,
                response_time_ms=100,
                is_stream=False,
                request_path="/v1/chat/completions",
                resolved_target_model_id="target-model-a",
                ingress_request_id="ingress-123",
                attempt_number=2,
                provider_correlation_id="resp_123",
                error_detail="upstream failed",
            )

        assert request_log_id == 41
        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()
        assert captured_entry["entry"].resolved_target_model_id == "target-model-a"
        assert captured_entry["entry"].ingress_request_id == "ingress-123"
        assert captured_entry["entry"].attempt_number == 2
        assert captured_entry["entry"].provider_correlation_id == "resp_123"


class TestDEF002_ModelExtraction:
    """DEF-002 (P1): routing model must be extracted from request body only."""

    def test_extract_model_from_valid_body(self):
        body = json.dumps(
            {
                "model": "claude-sonnet-4",
                "messages": [{"role": "user", "content": "hi"}],
            }
        ).encode()
        assert extract_model_from_body(body) == "claude-sonnet-4"

    def test_extract_model_returns_none_when_missing(self):
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
        assert extract_model_from_body(body) is None

    def test_extract_model_returns_none_for_invalid_json(self):
        assert extract_model_from_body(b"not json") is None


class TestDEF003_AuthHeaderPerEndpoint:
    """DEF-003 (P1): auth header must be configurable per-endpoint via auth_type."""

    def _make_endpoint(self, auth_type=None, api_key="sk-test"):
        ep = MagicMock()
        ep.auth_type = auth_type
        ep.api_key = api_key
        return ep

    def test_openai_provider_uses_bearer_by_default(self):
        ep = self._make_endpoint()
        headers = build_upstream_headers(ep, "openai")
        assert headers["Authorization"] == "Bearer sk-test"
        assert "x-api-key" not in headers

    def test_anthropic_provider_uses_xapikey_by_default(self):
        ep = self._make_endpoint()
        headers = build_upstream_headers(ep, "anthropic")
        assert headers["x-api-key"] == "sk-test"
        assert "anthropic-version" in headers

    def test_anthropic_provider_strips_whitespace_from_api_key(self):
        ep = self._make_endpoint(api_key="\tmy-super-secret-password-123 \n")
        headers = build_upstream_headers(ep, "anthropic")
        assert headers["x-api-key"] == "my-super-secret-password-123"

    def test_anthropic_endpoint_with_openai_auth_override(self):
        ep = self._make_endpoint(auth_type="openai")
        headers = build_upstream_headers(ep, "anthropic")
        assert headers["Authorization"] == "Bearer sk-test"
        assert "x-api-key" not in headers

    def test_openai_endpoint_with_anthropic_auth_override(self):
        ep = self._make_endpoint(auth_type="anthropic")
        headers = build_upstream_headers(ep, "openai")
        assert headers["x-api-key"] == "sk-test"
        assert "anthropic-version" in headers

    def test_gemini_provider_uses_bearer_by_default(self):
        ep = self._make_endpoint()
        headers = build_upstream_headers(ep, "gemini")
        assert headers["Authorization"] == "Bearer sk-test"

    def test_auth_type_takes_precedence_over_provider_type(self):
        ep = self._make_endpoint(auth_type="openai")
        headers = build_upstream_headers(ep, "anthropic")
        assert "Authorization" in headers
        assert headers["Authorization"] == "Bearer sk-test"
        assert "x-api-key" not in headers


class TestDEF005_GeminiPathModelRewrite:
    """DEF-005 (P0): proxy must rewrite model ID in Gemini-style URL paths."""

    def test_rewrite_gemini_path(self):
        from app.routers.proxy import _rewrite_model_in_path

        result = _rewrite_model_in_path(
            "/v1beta/models/gemini-3-flash:generateContent",
            "gemini-3-flash",
            "gemini-3-flash-preview",
        )
        assert result == "/v1beta/models/gemini-3-flash-preview:generateContent"

    def test_rewrite_gemini_path_stream(self):
        from app.routers.proxy import _rewrite_model_in_path

        result = _rewrite_model_in_path(
            "/v1beta/models/gemini-3-flash:streamGenerateContent",
            "gemini-3-flash",
            "gemini-3-flash-preview",
        )
        assert result == "/v1beta/models/gemini-3-flash-preview:streamGenerateContent"

    def test_no_rewrite_when_same_model(self):
        from app.routers.proxy import _rewrite_model_in_path

        path = "/v1beta/models/gemini-3-flash:generateContent"
        result = _rewrite_model_in_path(path, "gemini-3-flash", "gemini-3-flash")
        assert result == path

    def test_non_gemini_path_unchanged(self):
        from app.routers.proxy import _extract_model_from_path

        assert _extract_model_from_path("/v1/chat/completions") is None

    def test_extract_model_from_gemini_path(self):
        from app.routers.proxy import _extract_model_from_path

        assert (
            _extract_model_from_path("/v1beta/models/gemini-3-flash:generateContent")
            == "gemini-3-flash"
        )

    def test_extract_model_from_other_gemini_model_scoped_path(self):
        from app.routers.proxy import _extract_model_from_path

        assert (
            _extract_model_from_path("/v1beta/models/gemini-3-flash:countTokens")
            == "gemini-3-flash"
        )

    def test_extract_model_ignores_non_beta_gemini_path(self):
        from app.routers.proxy import _extract_model_from_path

        assert (
            _extract_model_from_path("/v1/models/gemini-3-flash:generateContent")
            is None
        )

    def test_resolve_model_prefers_body_then_path(self):
        from app.routers.proxy import _resolve_model_id

        raw_body = json.dumps({"model": "gpt-4o-mini"}).encode("utf-8")
        assert (
            _resolve_model_id(
                raw_body,
                "/v1beta/models/gemini-3-flash:generateContent",
            )
            == "gpt-4o-mini"
        )

    def test_resolve_model_uses_path_when_body_missing_model(self):
        from app.routers.proxy import _resolve_model_id

        raw_body = json.dumps({"contents": [{"parts": [{"text": "hi"}]}]}).encode(
            "utf-8"
        )
        assert (
            _resolve_model_id(
                raw_body,
                "/v1beta/models/gemini-3-flash:generateContent",
            )
            == "gemini-3-flash"
        )


class TestDEF080_ProviderCorrelationExtraction:
    def test_openai_prefers_x_request_id_header_then_client_request_id(self):
        from app.routers.proxy_domains.attempt_outcome_reporting import (
            extract_provider_correlation_id,
        )

        assert (
            extract_provider_correlation_id(
                provider_type="openai",
                response_headers={"x-request-id": "req-openai-1"},
                response_body=None,
                request_headers={"X-Client-Request-Id": "client-1"},
            )
            == "req-openai-1"
        )
        assert (
            extract_provider_correlation_id(
                provider_type="openai",
                response_headers={},
                response_body=None,
                request_headers={"X-Client-Request-Id": "client-1"},
            )
            == "client-1"
        )

    def test_anthropic_prefers_request_id_header_then_error_body_request_id(self):
        from app.routers.proxy_domains.attempt_outcome_reporting import (
            extract_provider_correlation_id,
        )

        assert (
            extract_provider_correlation_id(
                provider_type="anthropic",
                response_headers={"request-id": "req-anthropic-1"},
                response_body=b'{"type":"error","request_id":"req-anthropic-body"}',
                request_headers={},
            )
            == "req-anthropic-1"
        )
        assert (
            extract_provider_correlation_id(
                provider_type="anthropic",
                response_headers={},
                response_body=b'{"type":"error","request_id":"req-anthropic-body"}',
                request_headers={},
            )
            == "req-anthropic-body"
        )

    def test_gemini_extracts_response_id_from_body_or_stream_chunk(self):
        from app.routers.proxy_domains.attempt_outcome_reporting import (
            extract_provider_correlation_id,
        )

        assert (
            extract_provider_correlation_id(
                provider_type="gemini",
                response_headers={},
                response_body=b'{"responseId":"gemini-response-1"}',
                request_headers={},
            )
            == "gemini-response-1"
        )
        assert (
            extract_provider_correlation_id(
                provider_type="gemini",
                response_headers={},
                response_body=(
                    b'data: {"responseId":"gemini-stream-1","usageMetadata":{"totalTokenCount":3}}\n\n'
                ),
                request_headers={},
            )
            == "gemini-stream-1"
        )
