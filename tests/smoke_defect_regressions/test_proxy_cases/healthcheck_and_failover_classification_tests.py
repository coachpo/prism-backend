import asyncio
import json
import logging
from typing import AsyncGenerator, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.models import Connection, Endpoint
from app.services.proxy_service import (
    extract_model_from_body,
    build_upstream_headers,
)
from app.services.stats_service import log_request


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

        path, body = _build_health_check_request("gemini", "gemini-3.1-pro-preview")
        payload = cast(dict[str, object], body)
        contents = cast(list[dict[str, object]], payload["contents"])
        parts = cast(list[dict[str, str]], contents[0]["parts"])
        generation_config = cast(dict[str, int], payload["generationConfig"])

        assert path == "/v1beta/models/gemini-3.1-pro-preview:generateContent"
        assert parts[0]["text"] == "hi"
        assert generation_config["maxOutputTokens"] == 1

    def test_cross_provider_model_id_still_uses_provider_native_path(self):
        from app.routers.connections import _build_health_check_request

        path, body = _build_health_check_request("anthropic", "gemini-3.1-pro-preview")

        assert path == "/v1/messages"
        assert body["model"] == "gemini-3.1-pro-preview"


class TestDEF066_OpenAIHealthCheckFallback:
    """DEF-066 (P1): OpenAI health checks should try responses-basic fallback before legacy."""

    @pytest.mark.asyncio
    async def test_openai_health_check_skips_legacy_fallback_when_primary_is_healthy(
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
            execute_mock.return_value = ("healthy", "Connection successful", 6)
            (
                health_status,
                detail,
                response_time_ms,
                log_url,
            ) = await _probe_connection_health(
                client=AsyncMock(),
                connection=cast(Connection, connection),
                endpoint=cast(Endpoint, endpoint),
                provider_type="openai",
                model_id="gpt-4o-mini",
                headers={},
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
            (
                health_status,
                detail,
                response_time_ms,
                log_url,
            ) = await _probe_connection_health(
                client=AsyncMock(),
                connection=cast(Connection, connection),
                endpoint=cast(Endpoint, endpoint),
                provider_type="openai",
                model_id="gpt-4o-mini",
                headers={},
            )

        assert health_status == "healthy"
        assert detail == "Connection successful (fallback /v1/responses basic input)"
        assert response_time_ms == 5
        assert log_url == "https://api.openai.com/v1/responses"
        assert execute_mock.await_count == 2
        assert (
            execute_mock.await_args_list[0]
            .kwargs["upstream_url"]
            .endswith("/v1/responses")
        )
        assert (
            execute_mock.await_args_list[1]
            .kwargs["upstream_url"]
            .endswith("/v1/responses")
        )
        assert (
            execute_mock.await_args_list[0].kwargs["body"]["input"][0]["content"][0][
                "text"
            ]
            == "hi"
        )
        assert execute_mock.await_args_list[0].kwargs["body"]["max_output_tokens"] == 1
        assert execute_mock.await_args_list[1].kwargs["body"]["input"] == "hi"

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
            (
                health_status,
                detail,
                response_time_ms,
                log_url,
            ) = await _probe_connection_health(
                client=AsyncMock(),
                connection=cast(Connection, connection),
                endpoint=cast(Endpoint, endpoint),
                provider_type="openai",
                model_id="gpt-4o-mini",
                headers={},
            )

        assert health_status == "healthy"
        assert detail == "Connection successful (legacy fallback /v1/chat/completions)"
        assert response_time_ms == 4
        assert log_url == "https://api.openai.com/v1/chat/completions"
        assert execute_mock.await_count == 3
        assert (
            execute_mock.await_args_list[0]
            .kwargs["upstream_url"]
            .endswith("/v1/responses")
        )
        assert (
            execute_mock.await_args_list[1]
            .kwargs["upstream_url"]
            .endswith("/v1/responses")
        )
        assert (
            execute_mock.await_args_list[2]
            .kwargs["upstream_url"]
            .endswith("/v1/chat/completions")
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
            (
                health_status,
                detail,
                response_time_ms,
                log_url,
            ) = await _probe_connection_health(
                client=AsyncMock(),
                connection=cast(Connection, connection),
                endpoint=cast(Endpoint, endpoint),
                provider_type="anthropic",
                model_id="claude-sonnet-4",
                headers={},
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

    def test_validation_rejects_generic_openai_path_for_gemini(self):
        from app.routers.proxy import _validate_provider_path_compatibility

        with pytest.raises(HTTPException) as exc_info:
            _validate_provider_path_compatibility(
                "gemini",
                "/v1/chat/completions",
            )

        assert exc_info.value.status_code == 400
        assert "incompatible with provider 'gemini'" in exc_info.value.detail

    def test_validation_rejects_non_beta_gemini_native_path_for_gemini(self):
        from app.routers.proxy import _validate_provider_path_compatibility

        with pytest.raises(HTTPException) as exc_info:
            _validate_provider_path_compatibility(
                "gemini",
                "/v1/models/gemini-3.1-pro-preview:generateContent",
            )

        assert exc_info.value.status_code == 400
        assert "incompatible with provider 'gemini'" in exc_info.value.detail

    def test_validation_allows_gemini_native_path_for_gemini(self):
        from app.routers.proxy import _validate_provider_path_compatibility

        _validate_provider_path_compatibility(
            "gemini",
            "/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent",
        )

    def test_validation_allows_other_gemini_model_scoped_path_for_gemini(self):
        from app.routers.proxy import _validate_provider_path_compatibility

        _validate_provider_path_compatibility(
            "gemini",
            "/v1beta/models/gemini-3.1-pro-preview:countTokens",
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
    def test_classify_http_failure_marks_403_auth_like_when_body_matches_auth_patterns(
        self,
    ):
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

        failure_kind = _classify_failover_failure(
            exception=httpx.TimeoutException("timeout")
        )

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
