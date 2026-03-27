import asyncio
import json
import logging
from types import SimpleNamespace
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
    """DEF-059 (P0): health checks must use api-family-native paths and payloads."""

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

    def test_cross_vendor_model_id_still_uses_api_family_native_path(self):
        from app.routers.connections import _build_health_check_request

        path, body = _build_health_check_request("anthropic", "gemini-3.1-pro-preview")

        assert path == "/v1/messages"
        assert body["model"] == "gemini-3.1-pro-preview"

    @pytest.mark.asyncio
    async def test_health_route_uses_model_api_family_even_when_vendor_metadata_differs(
        self,
    ):
        from fastapi import FastAPI
        from starlette.requests import Request

        from app.routers.connections_domains.health_route_handlers import (
            perform_connection_health_check,
        )

        app = FastAPI()
        app.state.http_client = AsyncMock()
        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "path": "/api/connections/1/health-check",
                "raw_path": b"/api/connections/1/health-check",
                "query_string": b"",
                "headers": [(b"host", b"testserver")],
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
                "scheme": "http",
                "app": app,
            }
        )

        vendor = MagicMock()
        vendor.id = 19
        legacy_provider = MagicMock()
        legacy_provider.id = 19
        legacy_provider.key = "openai"

        endpoint = MagicMock()
        endpoint.id = 501

        connection = MagicMock()
        connection.id = 1001
        connection.endpoint_id = 501
        connection.endpoint_rel = endpoint
        connection.model_config_rel = MagicMock(
            api_family="anthropic",
            model_id="claude-sonnet-4-5",
            vendor=vendor,
            provider=legacy_provider,
        )

        build_upstream_headers_fn = MagicMock(return_value={"x-api-key": "sk-test"})
        probe_connection_health_fn = AsyncMock(
            return_value=("healthy", "ok", 7, "https://api.anthropic.com/v1/messages")
        )
        record_connection_recovery_fn = AsyncMock()
        db = AsyncMock()
        db.flush = AsyncMock()

        with (
            patch(
                "app.routers.connections_domains.health_route_handlers._load_health_check_connection_or_404",
                AsyncMock(return_value=connection),
            ),
            patch(
                "app.routers.connections_domains.health_route_handlers._load_enabled_blocklist_rules",
                AsyncMock(return_value=[]),
            ),
        ):
            response = await perform_connection_health_check(
                connection_id=connection.id,
                request=request,
                db=db,
                profile_id=1,
                build_upstream_headers_fn=build_upstream_headers_fn,
                probe_connection_health_fn=probe_connection_health_fn,
                record_connection_recovery_fn=record_connection_recovery_fn,
            )

        assert response.health_status == "healthy"
        assert build_upstream_headers_fn.call_args.args[1] == "anthropic"
        assert probe_connection_health_fn.await_args is not None
        assert probe_connection_health_fn.await_args.kwargs["api_family"] == "anthropic"


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
                connection=cast(Connection, cast(object, connection)),
                endpoint=cast(Endpoint, cast(object, endpoint)),
                api_family="openai",
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
                connection=cast(Connection, cast(object, connection)),
                endpoint=cast(Endpoint, cast(object, endpoint)),
                api_family="openai",
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
                connection=cast(Connection, cast(object, connection)),
                endpoint=cast(Endpoint, cast(object, endpoint)),
                api_family="openai",
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
                connection=cast(Connection, cast(object, connection)),
                endpoint=cast(Endpoint, cast(object, endpoint)),
                api_family="anthropic",
                model_id="claude-sonnet-4",
                headers={},
            )

        assert health_status == "unhealthy"
        assert detail == "HTTP 500"
        assert response_time_ms == 7
        assert log_url == "https://api.anthropic.com/v1/messages"
        assert execute_mock.await_count == 1


class TestDEF060_ProxyApiFamilyPathValidation:
    """DEF-060 (P0): proxy must fail fast on api-family/path mismatch."""

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
        from app.routers.proxy import _validate_api_family_path_compatibility

        with pytest.raises(HTTPException) as exc_info:
            _validate_api_family_path_compatibility(
                "anthropic",
                "/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent",
            )

        assert exc_info.value.status_code == 400
        assert "incompatible with api_family 'anthropic'" in exc_info.value.detail

    def test_validation_rejects_anthropic_messages_path_for_openai(self):
        from app.routers.proxy import _validate_api_family_path_compatibility

        with pytest.raises(HTTPException) as exc_info:
            _validate_api_family_path_compatibility(
                "openai",
                "/v1/messages",
            )

        assert exc_info.value.status_code == 400
        assert "incompatible with api_family 'openai'" in exc_info.value.detail

    def test_validation_rejects_generic_openai_path_for_anthropic(self):
        from app.routers.proxy import _validate_api_family_path_compatibility

        with pytest.raises(HTTPException) as exc_info:
            _validate_api_family_path_compatibility(
                "anthropic",
                "/v1/chat/completions",
            )

        assert exc_info.value.status_code == 400
        assert "incompatible with api_family 'anthropic'" in exc_info.value.detail

    def test_validation_rejects_generic_openai_path_for_gemini(self):
        from app.routers.proxy import _validate_api_family_path_compatibility

        with pytest.raises(HTTPException) as exc_info:
            _validate_api_family_path_compatibility(
                "gemini",
                "/v1/chat/completions",
            )

        assert exc_info.value.status_code == 400
        assert "incompatible with api_family 'gemini'" in exc_info.value.detail

    def test_validation_rejects_non_beta_gemini_native_path_for_gemini(self):
        from app.routers.proxy import _validate_api_family_path_compatibility

        with pytest.raises(HTTPException) as exc_info:
            _validate_api_family_path_compatibility(
                "gemini",
                "/v1/models/gemini-3.1-pro-preview:generateContent",
            )

        assert exc_info.value.status_code == 400
        assert "incompatible with api_family 'gemini'" in exc_info.value.detail

    def test_validation_allows_gemini_native_path_for_gemini(self):
        from app.routers.proxy import _validate_api_family_path_compatibility

        _validate_api_family_path_compatibility(
            "gemini",
            "/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent",
        )

    def test_validation_allows_other_gemini_model_scoped_path_for_gemini(self):
        from app.routers.proxy import _validate_api_family_path_compatibility

        _validate_api_family_path_compatibility(
            "gemini",
            "/v1beta/models/gemini-3.1-pro-preview:countTokens",
        )

    @pytest.mark.asyncio
    async def test_prepare_proxy_request_uses_model_api_family_not_vendor_metadata_for_path_compatibility(
        self,
    ):
        from app.routers.proxy_domains.request_setup import prepare_proxy_request

        request_path = "/v1/messages"
        request = self._build_request(request_path)
        raw_body = json.dumps(
            {
                "model": "claude-sonnet-4-5",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            }
        ).encode("utf-8")

        provider = MagicMock()
        provider.key = "openai"
        provider.audit_enabled = False
        provider.audit_capture_bodies = False
        provider.id = 1

        vendor = MagicMock()
        vendor.id = 99
        vendor.audit_enabled = False
        vendor.audit_capture_bodies = False

        connection = SimpleNamespace(endpoint_id=501)
        model_config = MagicMock()
        model_config.provider = provider
        model_config.vendor = vendor
        model_config.api_family = "anthropic"
        model_config.model_id = "claude-sonnet-4-5"
        model_config.loadbalance_strategy = SimpleNamespace(
            strategy_type="single",
            failover_recovery_enabled=False,
            failover_status_codes=[403, 422, 429, 500, 502, 503, 504, 529],
        )

        mock_rules_result = MagicMock()
        mock_rules_result.scalars.return_value.all.return_value = []
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_rules_result)

        with (
            patch(
                "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                AsyncMock(return_value=model_config),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.build_attempt_plan",
                AsyncMock(
                    return_value=SimpleNamespace(
                        connections=[connection], probe_eligible_connection_ids=[]
                    )
                ),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.load_costing_settings",
                AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.compute_cost_fields",
                return_value={},
            ),
        ):
            setup = await prepare_proxy_request(
                request=request,
                db=mock_db,
                raw_body=raw_body,
                request_path=request_path,
                profile_id=1,
            )

        assert setup.api_family == "anthropic"
        assert setup.effective_request_path == request_path

    @pytest.mark.asyncio
    async def test_handle_proxy_fails_before_upstream_attempt_on_mismatch(self):
        from app.routers.proxy import _handle_proxy

        request_path = "/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent"
        request = self._build_request(request_path)
        raw_body = json.dumps(
            {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
        ).encode("utf-8")

        provider = MagicMock()
        provider.key = "openai"
        provider.audit_enabled = False
        provider.audit_capture_bodies = False
        provider.id = 1

        vendor = MagicMock()
        vendor.id = 99
        vendor.audit_enabled = False
        vendor.audit_capture_bodies = False

        model_config = MagicMock()
        model_config.provider = provider
        model_config.vendor = vendor
        model_config.api_family = "anthropic"
        model_config.model_id = "gemini-3.1-pro-preview"
        requested_model_result = MagicMock()
        requested_model_result.scalars.return_value.one_or_none.return_value = None
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=requested_model_result)

        with (
            patch(
                "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                AsyncMock(return_value=model_config),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.build_attempt_plan"
            ) as attempt_plan_mock,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await _handle_proxy(
                    request=request,
                    db=mock_db,
                    raw_body=raw_body,
                    request_path=request_path,
                    profile_id=1,
                )

        assert exc_info.value.status_code == 400
        assert "incompatible with api_family 'anthropic'" in exc_info.value.detail
        attempt_plan_mock.assert_not_called()


class TestDEF061_FailoverFailureClassification:
    def test_classify_http_failure_treats_403_as_transient_http_when_body_matches_auth_patterns(
        self,
    ):
        from app.routers.proxy import _classify_http_failure

        raw_body = json.dumps(
            {"error": {"message": "Invalid API key provided", "type": "auth_error"}}
        ).encode("utf-8")

        assert _classify_http_failure(403, raw_body) == "transient_http"

    def test_classify_http_failure_treats_403_as_transient_http_for_spaced_api_key_message(
        self,
    ):
        from app.routers.proxy import _classify_http_failure

        raw_body = json.dumps(
            {"error": {"message": "invalid API key for this endpoint"}}
        ).encode("utf-8")

        assert _classify_http_failure(403, raw_body) == "transient_http"

    def test_classify_http_failure_marks_403_transient_without_auth_signal(self):
        from app.routers.proxy import _classify_http_failure

        raw_body = json.dumps({"error": {"message": "capacity issue"}}).encode("utf-8")

        assert _classify_http_failure(403, raw_body) == "transient_http"

    def test_classify_http_failure_non_403_is_transient_http(self):
        from app.routers.proxy import _classify_http_failure

        assert _classify_http_failure(429, None) == "transient_http"

    def test_should_failover_respects_explicit_failover_status_codes(self):
        from app.services.proxy_service import should_failover

        assert should_failover(422, [422, 503]) is True
        assert should_failover(403, [422, 503]) is False
        assert should_failover(403, [403, 422, 503]) is True

    def test_failure_kind_literal_excludes_auth_like(self):
        from typing import get_args

        from app.services.loadbalancer.types import FailureKind

        assert "auth_like" not in get_args(FailureKind)

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

        assert failure_kind == "transient_http"

    def test_recovery_success_status_classifies_2xx_and_3xx_as_success(self):
        from app.routers.proxy import _is_recovery_success_status

        assert _is_recovery_success_status(200) is True
        assert _is_recovery_success_status(302) is True
        assert _is_recovery_success_status(399) is True
        assert _is_recovery_success_status(400) is False
        assert _is_recovery_success_status(503) is False
