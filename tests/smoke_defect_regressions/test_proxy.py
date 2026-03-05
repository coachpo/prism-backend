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

class TestDEF059_HealthCheckRequestBuilder:
    """DEF-059 (P0): health checks must use provider-native paths and payloads."""

    def test_openai_health_check_uses_responses_endpoint(self):
        from app.routers.connections import _build_health_check_request

        path, body = _build_health_check_request("openai", "gpt-4o-mini")

        assert path == "/v1/responses"
        assert body == {
            "model": "gpt-4o-mini",
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hi"}],
                }
            ],
            "max_output_tokens": 1,
        }

    def test_openai_legacy_health_check_uses_chat_completions_endpoint(self):
        from app.routers.connections import _build_openai_legacy_health_check_request

        path, body = _build_openai_legacy_health_check_request("gpt-4o-mini")

        assert path == "/v1/chat/completions"
        assert body == {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }

    def test_openai_responses_basic_fallback_uses_string_input(self):
        from app.routers.connections import (
            _build_openai_responses_basic_health_check_request,
        )

        path, body = _build_openai_responses_basic_health_check_request("gpt-4o-mini")

        assert path == "/v1/responses"
        assert body == {
            "model": "gpt-4o-mini",
            "input": "hi",
        }

    def test_gemini_health_check_uses_generate_content_endpoint(self):
        from app.routers.connections import _build_health_check_request

        path, body = _build_health_check_request(
            "gemini", "gemini-3.1-pro-preview"
        )

        assert path == "/v1beta/models/gemini-3.1-pro-preview:generateContent"
        assert body["contents"][0]["parts"][0]["text"] == "hi"
        assert body["generationConfig"]["maxOutputTokens"] == 1

    def test_cross_provider_model_id_still_uses_provider_native_path(self):
        from app.routers.connections import _build_health_check_request

        path, body = _build_health_check_request(
            "anthropic", "gemini-3.1-pro-preview"
        )

        assert path == "/v1/messages"
        assert body["model"] == "gemini-3.1-pro-preview"

class TestDEF066_OpenAIHealthCheckFallback:
    """DEF-066 (P1): OpenAI health checks should try responses-basic fallback before legacy."""

    @pytest.mark.asyncio
    async def test_openai_health_check_skips_legacy_fallback_when_primary_is_healthy(self):
        from types import SimpleNamespace
        from app.routers.connections import _probe_connection_health

        connection = SimpleNamespace(base_url="https://api.openai.com")
        endpoint = SimpleNamespace(base_url="https://api.openai.com")

        with patch(
            "app.routers.connections._execute_health_check_request",
            new_callable=AsyncMock,
        ) as execute_mock:
            execute_mock.return_value = ("healthy", "Connection successful", 6)
            health_status, detail, response_time_ms, log_url = (
                await _probe_connection_health(
                    client=AsyncMock(),
                    connection=connection,
                    endpoint=endpoint,
                    provider_type="openai",
                    model_id="gpt-4o-mini",
                    headers={},
                )
            )

        assert health_status == "healthy"
        assert detail == "Connection successful"
        assert response_time_ms == 6
        assert log_url == "https://api.openai.com/v1/responses"
        assert execute_mock.await_count == 1

    @pytest.mark.asyncio
    async def test_openai_health_check_uses_responses_basic_fallback_when_primary_fails(
        self,
    ):
        from types import SimpleNamespace
        from app.routers.connections import _probe_connection_health

        connection = SimpleNamespace(base_url="https://api.openai.com")
        endpoint = SimpleNamespace(base_url="https://api.openai.com")

        with patch(
            "app.routers.connections._execute_health_check_request",
            new_callable=AsyncMock,
        ) as execute_mock:
            execute_mock.side_effect = [
                ("unhealthy", "HTTP 404", 8),
                ("healthy", "Connection successful", 5),
            ]
            health_status, detail, response_time_ms, log_url = (
                await _probe_connection_health(
                    client=AsyncMock(),
                    connection=connection,
                    endpoint=endpoint,
                    provider_type="openai",
                    model_id="gpt-4o-mini",
                    headers={},
                )
            )

        assert health_status == "healthy"
        assert detail == "Connection successful (fallback /v1/responses basic input)"
        assert response_time_ms == 5
        assert log_url == "https://api.openai.com/v1/responses"
        assert execute_mock.await_count == 2
        assert execute_mock.await_args_list[0].kwargs["upstream_url"].endswith(
            "/v1/responses"
        )
        assert execute_mock.await_args_list[1].kwargs["upstream_url"].endswith(
            "/v1/responses"
        )
        assert (
            execute_mock.await_args_list[0].kwargs["body"]["input"][0]["content"][0][
                "text"
            ]
            == "hi"
        )
        assert execute_mock.await_args_list[0].kwargs["body"]["max_output_tokens"] == 1
        assert (
            execute_mock.await_args_list[1].kwargs["body"]["input"]
            == "hi"
        )

    @pytest.mark.asyncio
    async def test_openai_health_check_uses_legacy_fallback_when_responses_fallback_fails(
        self,
    ):
        from types import SimpleNamespace
        from app.routers.connections import _probe_connection_health

        connection = SimpleNamespace(base_url="https://api.openai.com")
        endpoint = SimpleNamespace(base_url="https://api.openai.com")

        with patch(
            "app.routers.connections._execute_health_check_request",
            new_callable=AsyncMock,
        ) as execute_mock:
            execute_mock.side_effect = [
                ("unhealthy", "HTTP 404", 8),
                ("unhealthy", "HTTP 400", 6),
                ("healthy", "Connection successful", 4),
            ]
            health_status, detail, response_time_ms, log_url = (
                await _probe_connection_health(
                    client=AsyncMock(),
                    connection=connection,
                    endpoint=endpoint,
                    provider_type="openai",
                    model_id="gpt-4o-mini",
                    headers={},
                )
            )

        assert health_status == "healthy"
        assert detail == "Connection successful (legacy fallback /v1/chat/completions)"
        assert response_time_ms == 4
        assert log_url == "https://api.openai.com/v1/chat/completions"
        assert execute_mock.await_count == 3
        assert execute_mock.await_args_list[0].kwargs["upstream_url"].endswith(
            "/v1/responses"
        )
        assert execute_mock.await_args_list[1].kwargs["upstream_url"].endswith(
            "/v1/responses"
        )
        assert execute_mock.await_args_list[2].kwargs["upstream_url"].endswith(
            "/v1/chat/completions"
        )

    @pytest.mark.asyncio
    async def test_non_openai_health_check_does_not_use_legacy_fallback(self):
        from types import SimpleNamespace
        from app.routers.connections import _probe_connection_health

        connection = SimpleNamespace(base_url="https://api.anthropic.com")
        endpoint = SimpleNamespace(base_url="https://api.anthropic.com")

        with patch(
            "app.routers.connections._execute_health_check_request",
            new_callable=AsyncMock,
        ) as execute_mock:
            execute_mock.return_value = ("unhealthy", "HTTP 500", 7)
            health_status, detail, response_time_ms, log_url = (
                await _probe_connection_health(
                    client=AsyncMock(),
                    connection=connection,
                    endpoint=endpoint,
                    provider_type="anthropic",
                    model_id="claude-sonnet-4",
                    headers={},
                )
            )

        assert health_status == "unhealthy"
        assert detail == "HTTP 500"
        assert response_time_ms == 7
        assert log_url == "https://api.anthropic.com/v1/messages"
        assert execute_mock.await_count == 1

class TestDEF060_ProxyProviderPathValidation:
    """DEF-060 (P0): proxy must fail fast on provider/path mismatch."""

    @staticmethod
    def _build_request(path: str):
        from fastapi import FastAPI
        from starlette.requests import Request

        app = FastAPI()
        app.state.http_client = AsyncMock()
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "path": path,
                "raw_path": path.encode("utf-8"),
                "query_string": b"",
                "headers": [
                    (b"host", b"testserver"),
                    (b"content-type", b"application/json"),
                ],
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
                "scheme": "http",
                "app": app,
            }
        )

    def test_validation_rejects_gemini_native_path_for_anthropic(self):
        from app.routers.proxy import _validate_provider_path_compatibility

        with pytest.raises(HTTPException) as exc_info:
            _validate_provider_path_compatibility(
                "anthropic",
                "/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent",
            )

        assert exc_info.value.status_code == 400
        assert "incompatible with provider 'anthropic'" in exc_info.value.detail

    def test_validation_rejects_anthropic_messages_path_for_openai(self):
        from app.routers.proxy import _validate_provider_path_compatibility

        with pytest.raises(HTTPException) as exc_info:
            _validate_provider_path_compatibility(
                "openai",
                "/v1/messages",
            )

        assert exc_info.value.status_code == 400
        assert "incompatible with provider 'openai'" in exc_info.value.detail

    def test_validation_rejects_generic_openai_path_for_anthropic(self):
        from app.routers.proxy import _validate_provider_path_compatibility

        with pytest.raises(HTTPException) as exc_info:
            _validate_provider_path_compatibility(
                "anthropic",
                "/v1/chat/completions",
            )

        assert exc_info.value.status_code == 400
        assert "incompatible with provider 'anthropic'" in exc_info.value.detail

    def test_validation_allows_gemini_native_path_for_gemini(self):
        from app.routers.proxy import _validate_provider_path_compatibility

        _validate_provider_path_compatibility(
            "gemini",
            "/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent",
        )


    @pytest.mark.asyncio
    async def test_handle_proxy_fails_before_upstream_attempt_on_mismatch(self):
        from app.routers.proxy import _handle_proxy

        request_path = "/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent"
        request = self._build_request(request_path)
        raw_body = json.dumps(
            {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
        ).encode("utf-8")

        provider = MagicMock()
        provider.provider_type = "anthropic"
        provider.audit_enabled = False
        provider.audit_capture_bodies = False
        provider.id = 1

        model_config = MagicMock()
        model_config.provider = provider
        model_config.model_id = "gemini-3.1-pro-preview"

        with (
            patch(
                "app.routers.proxy.get_model_config_with_connections",
                AsyncMock(return_value=model_config),
            ),
            patch("app.routers.proxy.build_attempt_plan") as attempt_plan_mock,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await _handle_proxy(
                    request=request,
                    db=AsyncMock(),
                    raw_body=raw_body,
                    request_path=request_path,
                    profile_id=1,
                )

        assert exc_info.value.status_code == 400
        assert "incompatible with provider 'anthropic'" in exc_info.value.detail
        attempt_plan_mock.assert_not_called()

class TestDEF061_FailoverFailureClassification:
    def test_classify_http_failure_marks_403_auth_like_when_body_matches_auth_patterns(self):
        from app.routers.proxy import _classify_http_failure

        raw_body = json.dumps(
            {"error": {"message": "Invalid API key provided", "type": "auth_error"}}
        ).encode("utf-8")

        assert _classify_http_failure(403, raw_body) == "auth_like"

    def test_classify_http_failure_marks_403_auth_like_for_spaced_api_key_message(self):
        from app.routers.proxy import _classify_http_failure

        raw_body = json.dumps(
            {"error": {"message": "invalid API key for this endpoint"}}
        ).encode("utf-8")

        assert _classify_http_failure(403, raw_body) == "auth_like"

    def test_classify_http_failure_marks_403_transient_without_auth_signal(self):
        from app.routers.proxy import _classify_http_failure

        raw_body = json.dumps({"error": {"message": "capacity issue"}}).encode("utf-8")

        assert _classify_http_failure(403, raw_body) == "transient_http"

    def test_classify_http_failure_non_403_is_transient_http(self):
        from app.routers.proxy import _classify_http_failure

        assert _classify_http_failure(429, None) == "transient_http"

    def test_classify_failover_failure_for_timeout_exception(self):
        import httpx
        from app.routers.proxy import _classify_failover_failure

        failure_kind = _classify_failover_failure(exception=httpx.TimeoutException("timeout"))

        assert failure_kind == "timeout"

    def test_classify_failover_failure_for_connect_exception(self):
        import httpx
        from app.routers.proxy import _classify_failover_failure

        failure_kind = _classify_failover_failure(
            exception=httpx.ConnectError("connect fail")
        )

        assert failure_kind == "connect_error"

    def test_classify_failover_failure_uses_http_classifier(self):
        from app.routers.proxy import _classify_failover_failure

        raw_body = json.dumps({"error": {"message": "forbidden: bad token"}}).encode(
            "utf-8"
        )

        failure_kind = _classify_failover_failure(
            status_code=403,
            raw_body=raw_body,
        )

        assert failure_kind == "auth_like"

    def test_recovery_success_status_classifies_2xx_and_3xx_as_success(self):
        from app.routers.proxy import _is_recovery_success_status

        assert _is_recovery_success_status(200) is True
        assert _is_recovery_success_status(302) is True
        assert _is_recovery_success_status(399) is True
        assert _is_recovery_success_status(400) is False
        assert _is_recovery_success_status(503) is False


class TestDEF062_NonFailover4xxRecoveryState:
    @pytest.mark.asyncio
    async def test_non_failover_4xx_preserves_existing_recovery_state(self):
        from fastapi import FastAPI
        from starlette.requests import Request
        import httpx
        from app.routers.proxy import _handle_proxy
        from app.services.loadbalancer import _recovery_state

        class DummyHttpClient:
            def build_request(self, method: str, upstream_url: str, **kwargs):
                return httpx.Request(
                    method=method,
                    url=upstream_url,
                    headers=kwargs.get("headers"),
                    content=kwargs.get("content"),
                )

            async def send(self, request: httpx.Request, **kwargs):
                return httpx.Response(
                    status_code=404,
                    request=request,
                    headers={"content-type": "application/json"},
                    content=json.dumps(
                        {"error": {"message": "not found", "type": "invalid_request"}}
                    ).encode("utf-8"),
                )

        app = FastAPI()
        app.state.http_client = DummyHttpClient()
        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "path": "/v1/chat/completions",
                "raw_path": b"/v1/chat/completions",
                "query_string": b"",
                "headers": [
                    (b"host", b"testserver"),
                    (b"content-type", b"application/json"),
                ],
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
                "scheme": "http",
                "app": app,
            }
        )

        raw_body = json.dumps(
            {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
        ).encode("utf-8")

        provider = MagicMock()
        provider.provider_type = "openai"
        provider.audit_enabled = False
        provider.audit_capture_bodies = False
        provider.id = 1

        endpoint_rel = MagicMock()
        endpoint_rel.base_url = "https://api.openai.com/v1"

        connection = MagicMock()
        connection.id = 1001
        connection.endpoint_id = 501
        connection.endpoint_rel = endpoint_rel
        connection.pricing_template_rel = None
        connection.name = "primary"
        connection.custom_headers = None
        connection.auth_type = None

        model_config = MagicMock()
        model_config.provider = provider
        model_config.model_id = "gpt-4o-mini"
        model_config.lb_strategy = "failover"
        model_config.failover_recovery_enabled = True
        model_config.failover_recovery_cooldown_seconds = 60

        state_key = (1, connection.id)
        _recovery_state[state_key] = {
            "consecutive_failures": 3,
            "blocked_until_mono": 1234.0,
            "last_cooldown_seconds": 120.0,
            "last_failure_kind": "transient_http",
            "probe_eligible_logged": False,
        }

        try:
            mock_rules_result = MagicMock()
            mock_rules_result.scalars.return_value.all.return_value = []
            mock_db = AsyncMock()
            mock_db.execute = AsyncMock(return_value=mock_rules_result)

            with (
                patch(
                    "app.routers.proxy.get_model_config_with_connections",
                    AsyncMock(return_value=model_config),
                ),
                patch(
                    "app.routers.proxy.build_attempt_plan",
                    return_value=[connection],
                ),
                patch(
                    "app.routers.proxy._endpoint_is_active_now",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "app.routers.proxy.load_costing_settings",
                    AsyncMock(return_value=MagicMock()),
                ),
                patch("app.routers.proxy.compute_cost_fields", return_value={}),
                patch("app.routers.proxy.log_request", AsyncMock(return_value=901)),
                patch("app.routers.proxy.record_audit_log", AsyncMock()),
            ):
                response = await _handle_proxy(
                    request=request,
                    db=mock_db,
                    raw_body=raw_body,
                    request_path="/v1/chat/completions",
                    profile_id=1,
                )

            assert response.status_code == 404
            assert state_key in _recovery_state
            assert _recovery_state[state_key]["consecutive_failures"] == 3
            assert _recovery_state[state_key]["blocked_until_mono"] == 1234.0
        finally:
            _recovery_state.pop(state_key, None)
class TestDEF011_RuntimeEndpointActivityCheck:
    @pytest.mark.asyncio
    async def test_endpoint_is_active_now_returns_true_for_active_row(self):
        from app.routers.proxy import _endpoint_is_active_now

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = True

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        is_active = await _endpoint_is_active_now(mock_db, 7)
        assert is_active is True

    @pytest.mark.asyncio
    async def test_endpoint_is_active_now_returns_false_for_disabled_or_missing_row(
        self,
    ):
        from app.routers.proxy import _endpoint_is_active_now

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        is_active = await _endpoint_is_active_now(mock_db, 999)
        assert is_active is False

class TestDEF012_RuntimeEndpointToggleFailoverE2E:
    @pytest.mark.asyncio
    async def test_proxy_skips_endpoint_disabled_after_plan_and_uses_next_endpoint(self):
        import httpx
        from fastapi import FastAPI
        from sqlalchemy import select, update
        from starlette.requests import Request

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import Connection, Endpoint, ModelConfig, Profile, Provider
        from app.routers.proxy import _handle_proxy
        from app.services.loadbalancer import (
            _recovery_state,
            build_attempt_plan as real_build_attempt_plan,
        )

        # Prevent cross-loop pooled asyncpg connections from previous tests.
        await get_engine().dispose()
        class DummyHttpClient:
            def __init__(self):
                self.sent_urls: list[str] = []

            def build_request(self, method: str, upstream_url: str, **kwargs):
                return httpx.Request(
                    method=method,
                    url=upstream_url,
                    headers=kwargs.get("headers"),
                    content=kwargs.get("content"),
                )

            async def send(self, request: httpx.Request, **kwargs):
                self.sent_urls.append(str(request.url))
                return httpx.Response(
                    status_code=200,
                    request=request,
                    headers={"content-type": "application/json"},
                    content=json.dumps(
                        {
                            "id": "chatcmpl-ok",
                            "usage": {
                                "prompt_tokens": 1,
                                "completion_tokens": 1,
                                "total_tokens": 2,
                            },
                        }
                    ).encode("utf-8"),
                )

        try:
            async with AsyncSessionLocal() as seed_db:
                profile = Profile(
                    name=f"DEF012 Profile {uuid4().hex[:8]}",
                    is_active=False,
                    version=0,
                )
                provider = Provider(
                    name=f"OpenAI DEF012 {uuid4().hex[:8]}",
                    provider_type="openai",
                    audit_enabled=False,
                    audit_capture_bodies=False,
                )
                model = ModelConfig(
                    provider=provider,
                    profile=profile,
                    model_id="gpt-4o-mini-def012",
                    display_name="GPT-4o Mini DEF012",
                    model_type="native",
                    lb_strategy="failover",
                    failover_recovery_enabled=True,
                    failover_recovery_cooldown_seconds=60,
                    is_enabled=True,
                )
                primary_endpoint = Endpoint(
                    name="primary",
                    profile=profile,
                    base_url="https://primary.example.com/v1",
                    api_key="sk-primary",
                )
                secondary_endpoint = Endpoint(
                    name="secondary",
                    profile=profile,
                    base_url="https://secondary.example.com/v1",
                    api_key="sk-secondary",
                )
                primary = Connection(
                    model_config_rel=model,
                    profile=profile,
                    endpoint_rel=primary_endpoint,
                    is_active=True,
                    priority=0,
                    name="primary",
                )
                secondary = Connection(
                    model_config_rel=model,
                    profile=profile,
                    endpoint_rel=secondary_endpoint,
                    is_active=True,
                    priority=1,
                    name="secondary",
                )
                seed_db.add_all(
                    [
                        provider,
                        profile,
                        model,
                        primary_endpoint,
                        secondary_endpoint,
                        primary,
                        secondary,
                    ]
                )
                await seed_db.commit()
                await seed_db.refresh(primary)
                await seed_db.refresh(secondary)
                profile_id = profile.id
                primary_id = primary.id
                secondary_id = secondary.id

            async with AsyncSessionLocal() as db:
                client = DummyHttpClient()
                app = FastAPI()
                app.state.http_client = client
                request = Request(
                    {
                        "type": "http",
                        "http_version": "1.1",
                        "method": "POST",
                        "path": "/v1/chat/completions",
                        "raw_path": b"/v1/chat/completions",
                        "query_string": b"",
                        "headers": [
                            (b"host", b"testserver"),
                            (b"content-type", b"application/json"),
                        ],
                        "client": ("testclient", 50000),
                        "server": ("testserver", 80),
                        "scheme": "http",
                        "app": app,
                    }
                )

                raw_body = json.dumps(
                    {
                        "model": "gpt-4o-mini-def012",
                        "messages": [{"role": "user", "content": "hi"}],
                    }
                ).encode("utf-8")

                toggle_applied = False

                def build_plan_with_assert(profile_id, model_config, now_mono):
                    plan = real_build_attempt_plan(
                        profile_id, model_config, now_mono
                    )
                    assert [ep.id for ep in plan] == [primary_id, secondary_id]
                    return plan

                async def runtime_active_check(current_db, endpoint_id):
                    nonlocal toggle_applied
                    if endpoint_id == primary_id and not toggle_applied:
                        await current_db.execute(
                            update(Connection)
                            .where(Connection.id == primary_id)
                            .values(is_active=False)
                        )
                        await current_db.flush()
                        toggle_applied = True
                        return False

                    row = await current_db.execute(
                        select(Connection.is_active).where(Connection.id == endpoint_id)
                    )
                    active = row.scalar_one_or_none()
                    return bool(active) if active is not None else False

                with (
                    patch(
                        "app.routers.proxy.build_attempt_plan",
                        side_effect=build_plan_with_assert,
                    ),
                    patch(
                        "app.routers.proxy._endpoint_is_active_now",
                        AsyncMock(side_effect=runtime_active_check),
                    ),
                    patch("app.routers.proxy.log_request", AsyncMock(return_value=123)),
                ):
                    response = await _handle_proxy(
                        request=request,
                        db=db,
                        raw_body=raw_body,
                        request_path="/v1/chat/completions",
                        profile_id=profile_id,
                    )

                assert response.status_code == 200
                assert len(client.sent_urls) == 1
                assert "secondary.example.com" in client.sent_urls[0]

                primary_row = await db.execute(
                    select(Connection.is_active).where(Connection.id == primary_id)
                )
                secondary_row = await db.execute(
                    select(Connection.is_active).where(Connection.id == secondary_id)
                )
                assert primary_row.scalar_one() is False
                assert secondary_row.scalar_one() is True
        finally:
            _recovery_state.clear()

class TestDEF021_StreamingCancellationResilience:
    @staticmethod
    def _build_request(app, raw_body: bytes):
        from starlette.requests import Request

        async def receive_message():
            return {
                "type": "http.request",
                "body": raw_body,
                "more_body": False,
            }

        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "path": "/v1/responses",
                "raw_path": b"/v1/responses",
                "query_string": b"",
                "headers": [
                    (b"host", b"testserver"),
                    (b"content-type", b"application/json"),
                ],
                "client": ("testclient", 50001),
                "server": ("testserver", 80),
                "scheme": "http",
                "app": app,
            },
            receive=receive_message,
        )
        return request

    @staticmethod
    def _build_model_config_and_endpoint():
        provider = MagicMock()
        provider.provider_type = "openai"
        provider.audit_enabled = True
        provider.audit_capture_bodies = False
        provider.id = 11

        endpoint = MagicMock()
        endpoint.id = 201
        endpoint.endpoint_id = 201
        endpoint.base_url = "https://api.openai.com/v1"
        endpoint.api_key = "sk-test"
        endpoint.auth_type = None
        endpoint.name = "primary"

        connection = MagicMock()
        connection.id = 101
        connection.endpoint_id = 201
        connection.auth_type = None
        connection.name = "primary"
        connection.endpoint_rel = endpoint

        model_config = MagicMock()
        model_config.provider = provider
        model_config.model_id = "gpt-4o-mini"
        model_config.lb_strategy = "single"
        model_config.failover_recovery_enabled = False
        model_config.failover_recovery_cooldown_seconds = 60

        return model_config, connection

    @staticmethod
    def _build_db_mock():
        mock_rules_result = MagicMock()
        mock_rules_result.scalars.return_value.all.return_value = []
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_rules_result)
        return mock_db

    @staticmethod
    async def _wait_for_asyncmock_calls(
        mock_obj: AsyncMock, expected_min_calls: int = 1
    ):
        for _ in range(40):
            if mock_obj.await_count >= expected_min_calls:
                return
            await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_mid_stream_cancel_keeps_success_and_finalizes_logging(self, caplog):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from app.routers.proxy import _handle_proxy

        caplog.set_level(logging.ERROR)

        class CancelMidStreamResponse:
            def __init__(self):
                self.status_code = 200
                self.headers = {"content-type": "text/event-stream"}
                self.closed = False

            async def aiter_bytes(self):
                yield b'data: {"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
                raise asyncio.CancelledError()

            async def aclose(self):
                self.closed = True

        class DummyHttpClient:
            def __init__(self, upstream_resp):
                self._upstream_resp = upstream_resp

            def build_request(self, method: str, upstream_url: str, **kwargs):
                return httpx.Request(
                    method=method,
                    url=upstream_url,
                    headers=kwargs.get("headers"),
                    content=kwargs.get("content"),
                )

            async def send(self, request: httpx.Request, **kwargs):
                assert kwargs.get("stream") is True
                return self._upstream_resp

        app = FastAPI()
        upstream_resp = CancelMidStreamResponse()
        app.state.http_client = DummyHttpClient(upstream_resp)

        raw_body = json.dumps(
            {
                "model": "gpt-4o-mini",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            }
        ).encode("utf-8")
        request = self._build_request(app, raw_body)
        mock_db = self._build_db_mock()
        model_config, endpoint = self._build_model_config_and_endpoint()

        with (
            patch(
                "app.routers.proxy.get_model_config_with_connections",
                AsyncMock(return_value=model_config),
            ),
            patch("app.routers.proxy.build_attempt_plan", return_value=[endpoint]),
            patch(
                "app.routers.proxy._endpoint_is_active_now",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.routers.proxy.load_costing_settings",
                AsyncMock(return_value=MagicMock()),
            ),
            patch("app.routers.proxy.compute_cost_fields", return_value={}),
            patch(
                "app.routers.proxy.log_request", AsyncMock(return_value=501)
            ) as log_mock,
            patch("app.routers.proxy.record_audit_log", AsyncMock()) as audit_mock,
        ):
            response = await _handle_proxy(
                request=request,
                db=mock_db,
                raw_body=raw_body,
                request_path="/v1/responses",
                profile_id=1,
            )

            assert response.status_code == 200
            assert isinstance(response, StreamingResponse)
            stream = cast(AsyncGenerator[bytes, None], response.body_iterator)

            first = await stream.__anext__()
            assert first.startswith(b"data: ")

            with pytest.raises(asyncio.CancelledError):
                await stream.__anext__()

            await self._wait_for_asyncmock_calls(log_mock)
            await self._wait_for_asyncmock_calls(audit_mock)

            assert upstream_resp.closed is True
            log_mock.assert_awaited_once()
            audit_mock.assert_awaited_once()
            assert "Failed to log streaming request" not in caplog.text
            assert "Failed to record streaming audit log" not in caplog.text

    @pytest.mark.asyncio
    async def test_stream_generator_close_triggers_detached_finalize_without_error(
        self, caplog
    ):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from app.routers.proxy import _handle_proxy

        caplog.set_level(logging.ERROR)

        class SlowStreamResponse:
            def __init__(self):
                self.status_code = 200
                self.headers = {"content-type": "text/event-stream"}
                self.closed = False

            async def aiter_bytes(self):
                yield b'data: {"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
                await asyncio.sleep(1)

            async def aclose(self):
                self.closed = True

        class DummyHttpClient:
            def __init__(self, upstream_resp):
                self._upstream_resp = upstream_resp

            def build_request(self, method: str, upstream_url: str, **kwargs):
                return httpx.Request(
                    method=method,
                    url=upstream_url,
                    headers=kwargs.get("headers"),
                    content=kwargs.get("content"),
                )

            async def send(self, request: httpx.Request, **kwargs):
                assert kwargs.get("stream") is True
                return self._upstream_resp

        app = FastAPI()
        upstream_resp = SlowStreamResponse()
        app.state.http_client = DummyHttpClient(upstream_resp)

        raw_body = json.dumps(
            {
                "model": "gpt-4o-mini",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            }
        ).encode("utf-8")
        request = self._build_request(app, raw_body)
        mock_db = self._build_db_mock()
        model_config, endpoint = self._build_model_config_and_endpoint()

        with (
            patch(
                "app.routers.proxy.get_model_config_with_connections",
                AsyncMock(return_value=model_config),
            ),
            patch("app.routers.proxy.build_attempt_plan", return_value=[endpoint]),
            patch(
                "app.routers.proxy._endpoint_is_active_now",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.routers.proxy.load_costing_settings",
                AsyncMock(return_value=MagicMock()),
            ),
            patch("app.routers.proxy.compute_cost_fields", return_value={}),
            patch(
                "app.routers.proxy.log_request", AsyncMock(return_value=777)
            ) as log_mock,
            patch("app.routers.proxy.record_audit_log", AsyncMock()) as audit_mock,
        ):
            response = await _handle_proxy(
                request=request,
                db=mock_db,
                raw_body=raw_body,
                request_path="/v1/responses",
                profile_id=1,
            )

            assert response.status_code == 200
            assert isinstance(response, StreamingResponse)
            stream = cast(AsyncGenerator[bytes, None], response.body_iterator)

            first = await stream.__anext__()
            assert first.startswith(b"data: ")
            await stream.aclose()

            await self._wait_for_asyncmock_calls(log_mock)
            await self._wait_for_asyncmock_calls(audit_mock)

            assert upstream_resp.closed is True
            log_mock.assert_awaited_once()
            audit_mock.assert_awaited_once()
            assert "Failed to log streaming request" not in caplog.text
            assert "Failed to record streaming audit log" not in caplog.text

class TestDEF032_ProxyModelUpdateInvariants:
    @pytest.mark.asyncio
    async def test_update_model_renaming_native_cascades_proxy_redirects(self):
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import ModelConfig, Profile, Provider
        from app.routers.models import update_model
        from app.schemas.schemas import ModelConfigUpdate

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        native_model_id = f"def032-native-{suffix}"
        renamed_native_model_id = f"def032-native-renamed-{suffix}"
        proxy_model_id = f"def032-proxy-{suffix}"

        async with AsyncSessionLocal() as db:
            provider = (
                await db.execute(
                    select(Provider)
                    .where(Provider.provider_type == "openai")
                    .order_by(Provider.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if provider is None:
                provider = Provider(
                    name=f"DEF032 OpenAI {suffix}",
                    provider_type="openai",
                    description="DEF032 provider",
                )
                db.add(provider)
                await db.flush()

            profile = Profile(
                name=f"DEF032 Profile {suffix}",
                is_active=False,
                version=0,
            )
            db.add(profile)
            await db.flush()

            native_model = ModelConfig(
                profile_id=profile.id,
                provider_id=provider.id,
                model_id=native_model_id,
                model_type="native",
                redirect_to=None,
                lb_strategy="single",
                failover_recovery_enabled=True,
                failover_recovery_cooldown_seconds=60,
                is_enabled=True,
            )
            proxy_model = ModelConfig(
                profile_id=profile.id,
                provider_id=provider.id,
                model_id=proxy_model_id,
                model_type="proxy",
                redirect_to=native_model_id,
                lb_strategy="single",
                failover_recovery_enabled=True,
                failover_recovery_cooldown_seconds=60,
                is_enabled=True,
            )
            db.add_all([native_model, proxy_model])
            await db.flush()

            response = await update_model(
                model_config_id=native_model.id,
                body=ModelConfigUpdate(model_id=renamed_native_model_id),
                db=db,
                profile_id=profile.id,
            )
            await db.flush()
            await db.refresh(proxy_model)

            assert response.model_id == renamed_native_model_id
            assert proxy_model.redirect_to == renamed_native_model_id

    @pytest.mark.asyncio
    async def test_update_model_rejects_converting_connected_native_model_to_proxy(self):
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import Connection, Endpoint, ModelConfig, Profile, Provider
        from app.routers.models import update_model
        from app.schemas.schemas import ModelConfigUpdate

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        source_model_id = f"def032-source-{suffix}"
        target_model_id = f"def032-target-{suffix}"

        async with AsyncSessionLocal() as db:
            provider = (
                await db.execute(
                    select(Provider)
                    .where(Provider.provider_type == "openai")
                    .order_by(Provider.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if provider is None:
                provider = Provider(
                    name=f"DEF032 OpenAI {suffix}",
                    provider_type="openai",
                    description="DEF032 provider",
                )
                db.add(provider)
                await db.flush()

            profile = Profile(
                name=f"DEF032 Profile Connected {suffix}",
                is_active=False,
                version=0,
            )
            db.add(profile)
            await db.flush()

            source_model = ModelConfig(
                profile_id=profile.id,
                provider_id=provider.id,
                model_id=source_model_id,
                model_type="native",
                redirect_to=None,
                lb_strategy="single",
                failover_recovery_enabled=True,
                failover_recovery_cooldown_seconds=60,
                is_enabled=True,
            )
            target_model = ModelConfig(
                profile_id=profile.id,
                provider_id=provider.id,
                model_id=target_model_id,
                model_type="native",
                redirect_to=None,
                lb_strategy="single",
                failover_recovery_enabled=True,
                failover_recovery_cooldown_seconds=60,
                is_enabled=True,
            )
            db.add_all([source_model, target_model])
            await db.flush()

            endpoint = Endpoint(
                profile_id=profile.id,
                name=f"DEF032 endpoint {suffix}",
                base_url="https://api.openai.com/v1",
                api_key="sk-test",
            )
            db.add(endpoint)
            await db.flush()

            db.add(
                Connection(
                    profile_id=profile.id,
                    model_config_id=source_model.id,
                    endpoint_id=endpoint.id,
                    is_active=True,
                    priority=0,
                )
            )
            await db.flush()

            with pytest.raises(HTTPException) as exc_info:
                await update_model(
                    model_config_id=source_model.id,
                    body=ModelConfigUpdate(
                        model_type="proxy",
                        redirect_to=target_model_id,
                    ),
                    db=db,
                    profile_id=profile.id,
                )

            assert exc_info.value.status_code == 400
            assert "Delete connections first" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_update_model_rejects_proxy_self_redirect(self):
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import ModelConfig, Profile, Provider
        from app.routers.models import update_model
        from app.schemas.schemas import ModelConfigUpdate

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        model_id = f"def032-self-{suffix}"

        async with AsyncSessionLocal() as db:
            provider = (
                await db.execute(
                    select(Provider)
                    .where(Provider.provider_type == "openai")
                    .order_by(Provider.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if provider is None:
                provider = Provider(
                    name=f"DEF032 OpenAI {suffix}",
                    provider_type="openai",
                    description="DEF032 provider",
                )
                db.add(provider)
                await db.flush()

            profile = Profile(
                name=f"DEF032 Profile Self {suffix}",
                is_active=False,
                version=0,
            )
            db.add(profile)
            await db.flush()

            source_model = ModelConfig(
                profile_id=profile.id,
                provider_id=provider.id,
                model_id=model_id,
                model_type="native",
                redirect_to=None,
                lb_strategy="single",
                failover_recovery_enabled=True,
                failover_recovery_cooldown_seconds=60,
                is_enabled=True,
            )
            db.add(source_model)
            await db.flush()

            with pytest.raises(HTTPException) as exc_info:
                await update_model(
                    model_config_id=source_model.id,
                    body=ModelConfigUpdate(
                        model_type="proxy",
                        redirect_to=model_id,
                    ),
                    db=db,
                    profile_id=profile.id,
                )

            assert exc_info.value.status_code == 400
            assert exc_info.value.detail == "Proxy model cannot redirect to itself"
