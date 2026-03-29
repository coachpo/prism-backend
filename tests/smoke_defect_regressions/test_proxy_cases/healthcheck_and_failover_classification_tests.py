import asyncio
from datetime import datetime, timezone
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
            "input": "ping",
            "max_output_tokens": 1,
        }

    def test_openai_chat_completions_health_check_uses_chat_completions_endpoint(self):
        from app.routers.connections import (
            _build_openai_chat_completions_health_check_request,
        )

        path, body = _build_openai_chat_completions_health_check_request("gpt-4o-mini")

        assert path == "/v1/chat/completions"
        assert body == {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "ping"}],
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
            "input": "ping",
        }

    def test_gemini_health_check_uses_generate_content_endpoint(self):
        from app.routers.connections import _build_health_check_request

        path, body = _build_health_check_request("gemini", "gemini-3.1-pro-preview")
        payload = cast(dict[str, object], body)
        contents = cast(list[dict[str, object]], payload["contents"])
        parts = cast(list[dict[str, str]], contents[0]["parts"])
        generation_config = cast(dict[str, int], payload["generationConfig"])

        assert path == "/v1beta/models/gemini-3.1-pro-preview:generateContent"
        assert parts[0]["text"] == "ping"
        assert generation_config["maxOutputTokens"] == 1

    def test_cross_vendor_model_id_still_uses_api_family_native_path(self):
        from app.routers.connections import _build_health_check_request

        path, body = _build_health_check_request("anthropic", "gemini-3.1-pro-preview")

        assert path == "/v1/messages"
        assert body["model"] == "gemini-3.1-pro-preview"

    @pytest.mark.asyncio
    async def test_probe_runner_uses_model_api_family_even_when_vendor_metadata_differs(
        self,
    ):
        from app.services.monitoring_service import run_connection_probe

        vendor = MagicMock()
        vendor.id = 19
        vendor.key = "openai"

        endpoint = MagicMock()
        endpoint.id = 501
        endpoint.base_url = "https://api.anthropic.com"

        connection = MagicMock()
        connection.id = 1001
        connection.profile_id = 1
        connection.endpoint_id = 501
        connection.openai_probe_endpoint_variant = "responses"
        connection.endpoint_rel = endpoint
        connection.model_config_rel = MagicMock(
            id=301,
            api_family="anthropic",
            model_id="claude-sonnet-4-5",
            vendor=vendor,
            loadbalance_strategy=MagicMock(
                routing_policy={"kind": "adaptive", "monitoring": {"enabled": True}}
            ),
        )

        execute_probe_request_fn = AsyncMock(
            side_effect=[
                ("healthy", "Connection successful", 7),
                ("healthy", "Connection successful", 11),
            ]
        )

        result = await run_connection_probe(
            db=AsyncMock(),
            client=AsyncMock(),
            profile_id=1,
            connection_id=connection.id,
            load_connection_fn=AsyncMock(return_value=connection),
            load_blocklist_rules_fn=AsyncMock(return_value=[]),
            build_upstream_headers_fn=MagicMock(return_value={"x-api-key": "sk-test"}),
            execute_probe_request_fn=execute_probe_request_fn,
            record_probe_outcome_fn=AsyncMock(return_value="healthy"),
        )

        assert result.fused_status == "healthy"
        assert execute_probe_request_fn.await_count == 2
        assert (
            execute_probe_request_fn.await_args_list[0]
            .kwargs["upstream_url"]
            .endswith("/v1/messages")
        )
        assert (
            execute_probe_request_fn.await_args_list[1]
            .kwargs["upstream_url"]
            .endswith("/v1/messages")
        )


class TestMonitoringManualHealthChecksAndPersistence:
    @pytest.mark.asyncio
    async def test_health_route_delegates_to_shared_probe_runner(self):
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

        probe_result = SimpleNamespace(
            connection_id=123,
            checked_at=datetime.now(timezone.utc),
            health_status="healthy",
            detail="probe completed",
            conversation_delay_ms=17,
        )
        run_connection_probe_fn = AsyncMock(return_value=probe_result)

        response = await perform_connection_health_check(
            connection_id=123,
            request=request,
            db=AsyncMock(),
            profile_id=1,
            run_connection_probe_fn=run_connection_probe_fn,
        )

        assert response.connection_id == 123
        assert response.health_status == "healthy"
        assert response.response_time_ms == 17
        assert run_connection_probe_fn.await_args is not None
        assert run_connection_probe_fn.await_args.kwargs["connection_id"] == 123
        assert (
            run_connection_probe_fn.await_args.kwargs["client"] is app.state.http_client
        )

    @pytest.mark.asyncio
    async def test_create_connection_record_persists_openai_probe_endpoint_variant(
        self,
    ):
        from app.routers.connections_domains.crud_dependencies import (
            ConnectionCrudDependencies,
        )
        from app.routers.connections_domains.crud_handlers.creation import (
            create_connection_record,
        )
        from app.schemas.schemas import ConnectionCreate

        endpoint = SimpleNamespace(id=77)
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.flush = AsyncMock()
        deps = ConnectionCrudDependencies(
            clear_connection_state_fn=AsyncMock(),
            clear_round_robin_state_for_model_fn=AsyncMock(),
            create_endpoint_from_inline_fn=AsyncMock(),
            ensure_model_config_ids_exist_fn=AsyncMock(),
            list_ordered_connections_fn=AsyncMock(return_value=[]),
            list_ordered_connections_for_models_fn=AsyncMock(),
            load_connection_or_404_fn=AsyncMock(return_value=SimpleNamespace(id=1)),
            load_model_or_404_fn=AsyncMock(
                return_value=SimpleNamespace(api_family="openai")
            ),
            lock_profile_row_fn=AsyncMock(),
            normalize_connection_priorities_fn=MagicMock(),
            serialize_custom_headers_fn=MagicMock(return_value=None),
            validate_pricing_template_id_fn=AsyncMock(return_value=None),
        )

        with patch(
            "app.routers.connections_domains.crud_handlers.creation.resolve_create_endpoint",
            AsyncMock(return_value=endpoint),
        ):
            await create_connection_record(
                model_config_id=9,
                body=ConnectionCreate(
                    endpoint_id=endpoint.id,
                    openai_probe_endpoint_variant="chat_completions",
                ),
                db=mock_db,
                profile_id=1,
                deps=deps,
            )

        created_connection = mock_db.add.call_args.args[0]
        assert created_connection.openai_probe_endpoint_variant == "chat_completions"

    @pytest.mark.asyncio
    async def test_update_connection_record_persists_openai_probe_endpoint_variant(
        self,
    ):
        from app.routers.connections_domains.crud_dependencies import (
            ConnectionCrudDependencies,
        )
        from app.routers.connections_domains.crud_handlers.updating import (
            update_connection_record,
        )
        from app.schemas.schemas import ConnectionUpdate

        connection = SimpleNamespace(
            id=55,
            profile_id=1,
            endpoint_id=11,
            model_config_id=9,
            is_active=True,
            auth_type=None,
            custom_headers=None,
            updated_at=None,
        )
        deps = ConnectionCrudDependencies(
            clear_connection_state_fn=AsyncMock(),
            clear_round_robin_state_for_model_fn=AsyncMock(),
            create_endpoint_from_inline_fn=AsyncMock(),
            ensure_model_config_ids_exist_fn=AsyncMock(),
            list_ordered_connections_fn=AsyncMock(),
            list_ordered_connections_for_models_fn=AsyncMock(),
            load_connection_or_404_fn=AsyncMock(return_value=connection),
            load_model_or_404_fn=AsyncMock(
                return_value=SimpleNamespace(api_family="openai")
            ),
            lock_profile_row_fn=AsyncMock(),
            normalize_connection_priorities_fn=MagicMock(),
            serialize_custom_headers_fn=MagicMock(),
            validate_pricing_template_id_fn=AsyncMock(),
        )
        mock_db = AsyncMock()
        mock_db.flush = AsyncMock()

        with patch(
            "app.routers.connections_domains.crud_handlers.updating.build_connection_update_data",
            AsyncMock(
                return_value={"openai_probe_endpoint_variant": "chat_completions"}
            ),
        ):
            await update_connection_record(
                connection_id=connection.id,
                body=ConnectionUpdate(openai_probe_endpoint_variant="chat_completions"),
                db=mock_db,
                profile_id=1,
                deps=deps,
            )

        assert connection.openai_probe_endpoint_variant == "chat_completions"


class TestDEF066_OpenAIHealthCheckFallback:
    """DEF-066 (P1): OpenAI health checks should try responses-basic fallback before chat-completions fallback."""

    @pytest.mark.asyncio
    async def test_openai_health_check_skips_chat_completions_fallback_when_primary_is_healthy(
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
        assert execute_mock.await_args_list[0].kwargs["body"]["input"] == "ping"
        assert execute_mock.await_args_list[0].kwargs["body"]["max_output_tokens"] == 1
        assert execute_mock.await_args_list[1].kwargs["body"]["input"] == "ping"

    @pytest.mark.asyncio
    async def test_openai_health_check_uses_chat_completions_fallback_when_responses_fallback_fails(
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
        assert detail == "Connection successful (fallback /v1/chat/completions)"
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
    async def test_non_openai_health_check_does_not_use_chat_completions_fallback(self):
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

        vendor = MagicMock()
        vendor.id = 99
        vendor.key = "openai"
        vendor.audit_enabled = False
        vendor.audit_capture_bodies = False

        connection = SimpleNamespace(endpoint_id=501)
        model_config = MagicMock()
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

        vendor = MagicMock()
        vendor.id = 99
        vendor.key = "openai"
        vendor.audit_enabled = False
        vendor.audit_capture_bodies = False

        model_config = MagicMock()
        model_config.vendor = vendor
        model_config.api_family = "anthropic"
        model_config.model_id = "gemini-3.1-pro-preview"
        requested_model_result = MagicMock()
        requested_model_result.scalars.return_value.one_or_none.return_value = (
            model_config
        )
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

    def test_failure_kind_literal_matches_current_failure_kinds(self):
        from typing import get_args

        from app.services.loadbalancer.types import FailureKind

        assert set(get_args(FailureKind)) == {
            "transient_http",
            "connect_error",
            "timeout",
        }

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
