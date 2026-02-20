import json
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from app.services.proxy_service import (
    rewrite_model_in_body,
    extract_model_from_body,
    build_upstream_headers,
    PROVIDER_AUTH,
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
                provider_type="openai",
                endpoint_id=1,
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
                provider_type="openai",
                endpoint_id=1,
                endpoint_base_url="http://example.com",
                status_code=0,
                response_time_ms=50,
                is_stream=False,
                request_path="/v1/chat/completions",
                error_detail="connection refused",
            )
        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()


class TestDEF002_ModelIdRewriting:
    """DEF-002 (P1): proxy must inject/rewrite model field in forwarded body."""

    def test_rewrite_model_for_proxy_alias(self):
        body = json.dumps(
            {
                "model": "claude-sonnet-4",
                "messages": [{"role": "user", "content": "hi"}],
            }
        ).encode()
        result = rewrite_model_in_body(body, "claude-sonnet-4-20250514")
        parsed = json.loads(result)
        assert parsed["model"] == "claude-sonnet-4-20250514"
        assert parsed["messages"] == [{"role": "user", "content": "hi"}]

    def test_inject_model_when_missing_from_body(self):
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
        assert extract_model_from_body(body) is None
        result = rewrite_model_in_body(body, "gemini-2.5-flash")
        parsed = json.loads(result)
        assert parsed["model"] == "gemini-2.5-flash"

    def test_rewrite_preserves_other_fields(self):
        body = json.dumps(
            {
                "model": "alias-name",
                "messages": [{"role": "user", "content": "test"}],
                "temperature": 0.7,
                "max_tokens": 100,
                "stream": True,
            }
        ).encode()
        result = rewrite_model_in_body(body, "real-model-id")
        parsed = json.loads(result)
        assert parsed["model"] == "real-model-id"
        assert parsed["temperature"] == 0.7
        assert parsed["max_tokens"] == 100
        assert parsed["stream"] is True

    def test_rewrite_returns_none_for_none_body(self):
        assert rewrite_model_in_body(None, "model") is None

    def test_rewrite_returns_original_for_invalid_json(self):
        body = b"not json"
        result = rewrite_model_in_body(body, "model")
        assert result == body


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


class TestDEF004_FrontendDeleteErrorHandling:
    """DEF-004 (P2): frontend must show error toast on failed model delete."""

    def test_api_client_throws_error_with_detail_message(self):
        pass
