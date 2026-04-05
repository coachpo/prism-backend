import asyncio
import json
import logging
from types import SimpleNamespace
from typing import AsyncGenerator, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.models.models import Connection
from app.services.loadbalancer.policy import resolve_effective_loadbalance_policy
from app.services.loadbalancer.types import (
    AttemptCandidate,
    AttemptCandidateScoreInput,
    AttemptPlan,
)
from app.services.background_tasks import BackgroundTaskManager
from tests.loadbalance_strategy_helpers import make_routing_policy_adaptive


def _legacy_single_strategy() -> SimpleNamespace:
    return SimpleNamespace(
        strategy_type="legacy",
        legacy_strategy_type="single",
        auto_recovery={"mode": "disabled"},
        routing_policy=None,
    )


def _attempt_candidate(connection: object) -> AttemptCandidate:
    connection_id = getattr(connection, "id", getattr(connection, "endpoint_id", 0))
    return AttemptCandidate(
        connection=cast(Connection, connection),
        score_input=AttemptCandidateScoreInput(
            connection=cast(Connection, connection),
            circuit_state="closed",
            blocked_until_at=None,
            banned_until_at=None,
            probe_available_at=None,
            in_flight_non_stream=0,
            in_flight_stream=0,
            qps_window_count=0,
            live_p95_latency_ms=None,
            last_live_failure_kind=None,
            last_live_failure_at=None,
            last_live_success_at=None,
            last_probe_status=None,
            last_probe_at=None,
            endpoint_ping_ewma_ms=None,
            conversation_delay_ewma_ms=None,
        ),
        score=0.0,
        sort_key=(0.0, getattr(connection, "priority", 0), connection_id),
    )


def _attempt_plan(
    *connections: object,
    policy: object | None = None,
    probe_eligible_connection_ids: list[int] | None = None,
):
    resolved_policy = resolve_effective_loadbalance_policy(
        SimpleNamespace(
            routing_policy=policy
            or make_routing_policy_adaptive(deadline_budget_ms=30_000)
        )
    )
    return AttemptPlan(
        policy=resolved_policy,
        candidates=[_attempt_candidate(connection) for connection in connections],
        blocked_connection_ids=[],
        probe_eligible_connection_ids=probe_eligible_connection_ids or [],
    )


class TestDEF062_NonFailover4xxRecoveryState:
    @pytest.mark.asyncio
    async def test_prepare_proxy_request_captures_failover_policy_snapshot(self):
        from fastapi import FastAPI
        from starlette.requests import Request

        from app.routers.proxy_domains.request_setup import prepare_proxy_request

        app = FastAPI()
        app.state.http_client = object()
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

        vendor = SimpleNamespace(
            key="openai",
            audit_enabled=False,
            audit_capture_bodies=False,
            id=1,
        )
        strategy = SimpleNamespace(
            strategy_type="failover",
            failover_recovery_enabled=True,
            failover_cooldown_seconds=45,
            failover_failure_threshold=4,
            failover_backoff_multiplier=3.5,
            failover_max_cooldown_seconds=720,
            failover_jitter_ratio=0.35,
            failover_status_codes=[403, 422, 429, 500, 502, 503, 504, 529],
        )
        connection = SimpleNamespace(endpoint_id=501)
        model_config = SimpleNamespace(
            vendor=vendor,
            api_family="openai",
            model_id="gpt-4o-mini",
            loadbalance_strategy=strategy,
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
                    return_value=_attempt_plan(
                        connection,
                        policy=make_routing_policy_adaptive(
                            base_open_seconds=45,
                            failure_threshold=4,
                            backoff_multiplier=3.5,
                            max_open_seconds=720,
                            jitter_ratio=0.35,
                            failure_status_codes=[
                                403,
                                422,
                                429,
                                500,
                                502,
                                503,
                                504,
                                529,
                            ],
                        ),
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
                request_path="/v1/chat/completions",
                profile_id=1,
            )

        strategy.failover_cooldown_seconds = 999
        strategy.failover_failure_threshold = 9

        assert setup.failover_policy.failover_cooldown_seconds == 45
        assert setup.failover_policy.failover_failure_threshold == 4

    @pytest.mark.asyncio
    async def test_prepare_proxy_request_treats_gemini_stream_path_as_streaming_without_body_flag(
        self,
    ):
        from fastapi import FastAPI
        from starlette.requests import Request

        from app.routers.proxy_domains.request_setup import prepare_proxy_request

        app = FastAPI()
        app.state.http_client = object()
        request_path = "/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent"
        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "path": request_path,
                "raw_path": request_path.encode("utf-8"),
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
            {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
        ).encode("utf-8")

        vendor = SimpleNamespace(
            key="google",
            audit_enabled=False,
            audit_capture_bodies=False,
            id=1,
        )
        strategy = SimpleNamespace(
            strategy_type="failover",
            failover_recovery_enabled=True,
            failover_cooldown_seconds=45,
            failover_failure_threshold=4,
            failover_backoff_multiplier=3.5,
            failover_max_cooldown_seconds=720,
            failover_jitter_ratio=0.35,
            failover_status_codes=[403, 422, 429, 500, 502, 503, 504, 529],
        )
        connection = SimpleNamespace(endpoint_id=501)
        model_config = SimpleNamespace(
            vendor=vendor,
            api_family="gemini",
            model_id="gemini-3.1-pro-preview",
            loadbalance_strategy=strategy,
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
                AsyncMock(return_value=_attempt_plan(connection)),
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

        assert setup.is_streaming is True

    @pytest.mark.asyncio
    async def test_prepare_proxy_request_keeps_requested_proxy_vendor_metadata_for_audit_and_logs(
        self,
    ):
        from fastapi import FastAPI
        from starlette.requests import Request

        from app.routers.proxy_domains.request_setup import prepare_proxy_request

        app = FastAPI()
        app.state.http_client = object()
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
            {"model": "proxy-model", "messages": [{"role": "user", "content": "hi"}]}
        ).encode("utf-8")

        proxy_vendor = SimpleNamespace(
            key="openrouter",
            name="OpenRouter",
            audit_enabled=True,
            audit_capture_bodies=True,
            id=4,
        )
        native_vendor = SimpleNamespace(
            key="openai",
            name="OpenAI",
            audit_enabled=False,
            audit_capture_bodies=False,
            id=1,
        )
        strategy = SimpleNamespace(
            strategy_type="fill-first",
            failover_recovery_enabled=True,
            failover_cooldown_seconds=45,
            failover_failure_threshold=2,
            failover_backoff_multiplier=2.0,
            failover_max_cooldown_seconds=300,
            failover_jitter_ratio=0.1,
            failover_status_codes=[403, 422, 429, 500, 502, 503, 504, 529],
            failover_ban_mode="off",
            failover_max_cooldown_strikes_before_ban=0,
            failover_ban_duration_seconds=0,
        )
        connection = SimpleNamespace(endpoint_id=501)
        resolved_model_config = SimpleNamespace(
            vendor=native_vendor,
            api_family="openai",
            model_id="native-model",
            loadbalance_strategy=strategy,
        )
        requested_proxy_model = SimpleNamespace(
            vendor=proxy_vendor,
            api_family="openai",
            model_id="proxy-model",
            is_enabled=True,
        )

        mock_requested_model_result = MagicMock()
        mock_requested_model_result.scalars.return_value.one_or_none.return_value = (
            requested_proxy_model
        )
        mock_rules_result = MagicMock()
        mock_rules_result.scalars.return_value.all.return_value = []
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            side_effect=[mock_requested_model_result, mock_rules_result]
        )

        with (
            patch(
                "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                AsyncMock(return_value=resolved_model_config),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.build_attempt_plan",
                AsyncMock(return_value=_attempt_plan(connection)),
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
                request_path="/v1/chat/completions",
                profile_id=1,
            )

        assert setup.model_id == "proxy-model"
        assert setup.resolved_target_model_id == "native-model"
        assert setup.api_family == "openai"
        assert setup.vendor_id == 4
        assert setup.vendor_key == "openrouter"
        assert setup.vendor_name == "OpenRouter"
        assert setup.audit_enabled is True
        assert setup.audit_capture_bodies is True

    @pytest.mark.asyncio
    async def test_non_failover_4xx_preserves_existing_recovery_state(self):
        from fastapi import FastAPI
        from starlette.requests import Request
        import httpx
        from app.routers.proxy import _handle_proxy

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

        vendor = MagicMock()
        vendor.key = "openai"
        vendor.audit_enabled = False
        vendor.audit_capture_bodies = False
        vendor.id = 1

        endpoint_rel = MagicMock()
        endpoint_rel.base_url = "https://api.openai.com"

        connection = MagicMock()
        connection.id = 1001
        connection.endpoint_id = 501
        connection.endpoint_rel = endpoint_rel
        connection.pricing_template_rel = None
        connection.name = "primary"
        connection.custom_headers = None
        connection.auth_type = None

        model_config = MagicMock()
        model_config.vendor = vendor
        model_config.api_family = "openai"
        model_config.model_id = "gpt-4o-mini"
        model_config.loadbalance_strategy = SimpleNamespace(
            strategy_type="adaptive",
            routing_policy=make_routing_policy_adaptive(),
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
                AsyncMock(return_value=_attempt_plan(connection)),
            ),
            patch(
                "app.routers.proxy._endpoint_is_active_now",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.load_costing_settings",
                AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.compute_cost_fields",
                return_value={},
            ),
            patch("app.routers.proxy.log_request", AsyncMock(return_value=901)),
            patch("app.routers.proxy.record_audit_log", AsyncMock()),
            patch(
                "app.routers.proxy.record_connection_failure", AsyncMock()
            ) as mark_failed,
            patch(
                "app.routers.proxy.record_connection_recovery", AsyncMock()
            ) as mark_recovered,
        ):
            response = await _handle_proxy(
                request=request,
                db=mock_db,
                raw_body=raw_body,
                request_path="/v1/chat/completions",
                profile_id=1,
            )

        assert response.status_code == 404
        assert response.headers["content-type"] == "application/json"
        assert json.loads(bytes(response.body)) == {
            "error": {"message": "not found", "type": "invalid_request"}
        }
        mark_failed.assert_not_awaited()
        mark_recovered.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failover_failure_uses_request_scoped_policy_cooldown(self):
        from app.routers.proxy_domains.attempt_handlers import (
            _record_connection_failure_if_needed,
        )
        from app.routers.proxy_domains.attempt_types import (
            ProxyAttemptTarget,
            ProxyRequestState,
            ProxyRuntimeDependencies,
        )

        record_connection_failure = AsyncMock()
        deps = cast(
            ProxyRuntimeDependencies,
            cast(
                object,
                SimpleNamespace(record_connection_failure_fn=record_connection_failure),
            ),
        )
        state = cast(
            ProxyRequestState,
            cast(
                object,
                SimpleNamespace(
                    profile_id=7,
                    setup=SimpleNamespace(
                        recovery_active=True,
                        failover_policy=SimpleNamespace(
                            failover_recovery_enabled=True,
                            failover_cooldown_seconds=17.5,
                        ),
                        model_id="gpt-4o-mini",
                        vendor_id=1,
                    ),
                ),
            ),
        )
        target = cast(
            ProxyAttemptTarget,
            cast(
                object,
                SimpleNamespace(connection=SimpleNamespace(id=1001, endpoint_id=501)),
            ),
        )

        await _record_connection_failure_if_needed(
            deps=deps,
            state=state,
            target=target,
            status_code=500,
            raw_body=b'{"error": {"message": "retry"}}',
        )

        record_connection_failure.assert_awaited_once_with(
            7,
            1001,
            17.5,
            "transient_http",
            state.setup.failover_policy,
            "gpt-4o-mini",
            501,
            1,
            now_at=None,
        )

    @pytest.mark.asyncio
    async def test_failover_retry_reuses_ingress_request_id_and_increments_attempt_number(
        self,
    ):
        from fastapi import FastAPI
        import httpx
        from starlette.requests import Request

        from app.routers.proxy import _handle_proxy

        class DummyHttpClient:
            def __init__(self):
                self._responses = [
                    httpx.Response(
                        status_code=500,
                        headers={"content-type": "application/json"},
                        content=b'{"error":{"message":"retry"}}',
                    ),
                    httpx.Response(
                        status_code=200,
                        headers={"content-type": "application/json"},
                        content=b'{"id":"ok"}',
                    ),
                ]

            def build_request(self, method: str, upstream_url: str, **kwargs):
                return httpx.Request(
                    method=method,
                    url=upstream_url,
                    headers=kwargs.get("headers"),
                    content=kwargs.get("content"),
                )

            async def send(self, request: httpx.Request, **kwargs):
                response = self._responses.pop(0)
                response.request = request
                return response

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

        vendor = MagicMock()
        vendor.key = "openai"
        vendor.audit_enabled = False
        vendor.audit_capture_bodies = False
        vendor.id = 1

        first_endpoint = MagicMock()
        first_endpoint.base_url = "https://first.example.com/v1"
        second_endpoint = MagicMock()
        second_endpoint.base_url = "https://second.example.com/v1"

        first_connection = MagicMock()
        first_connection.id = 1001
        first_connection.endpoint_id = 501
        first_connection.endpoint_rel = first_endpoint
        first_connection.pricing_template_rel = None
        first_connection.name = "first"
        first_connection.custom_headers = None
        first_connection.auth_type = None

        second_connection = MagicMock()
        second_connection.id = 1002
        second_connection.endpoint_id = 502
        second_connection.endpoint_rel = second_endpoint
        second_connection.pricing_template_rel = None
        second_connection.name = "second"
        second_connection.custom_headers = None
        second_connection.auth_type = None

        model_config = MagicMock()
        model_config.vendor = vendor
        model_config.api_family = "openai"
        model_config.model_id = "gpt-4o-mini"
        model_config.loadbalance_strategy = SimpleNamespace(
            strategy_type="adaptive",
            routing_policy=make_routing_policy_adaptive(base_open_seconds=17),
        )

        mock_rules_result = MagicMock()
        mock_rules_result.scalars.return_value.all.return_value = []
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_rules_result)
        log_request = AsyncMock(side_effect=[901, 902])

        with (
            patch(
                "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                AsyncMock(return_value=model_config),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.build_attempt_plan",
                AsyncMock(
                    return_value=_attempt_plan(first_connection, second_connection)
                ),
            ),
            patch(
                "app.routers.proxy._endpoint_is_active_now",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.load_costing_settings",
                AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.compute_cost_fields",
                return_value={},
            ),
            patch("app.routers.proxy.log_request", log_request),
            patch("app.routers.proxy.record_audit_log", AsyncMock()),
            patch("app.routers.proxy.record_connection_failure", AsyncMock()),
            patch("app.routers.proxy.record_connection_recovery", AsyncMock()),
        ):
            response = await _handle_proxy(
                request=request,
                db=mock_db,
                raw_body=raw_body,
                request_path="/v1/chat/completions",
                profile_id=1,
            )

        assert response.status_code == 200
        assert log_request.await_count == 2
        first_call = log_request.await_args_list[0].kwargs
        second_call = log_request.await_args_list[1].kwargs
        assert first_call["attempt_number"] == 1
        assert second_call["attempt_number"] == 2
        assert first_call["ingress_request_id"] == second_call["ingress_request_id"]

    @pytest.mark.asyncio
    async def test_limiter_denial_spills_to_next_connection_without_upstream_attempt_on_first(
        self,
    ):
        from fastapi import FastAPI
        import httpx
        from starlette.requests import Request

        from app.routers.proxy import _handle_proxy
        from app.services.loadbalancer.limiter import LimiterAcquireResult

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
                    content=b'{"id":"ok"}',
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

        vendor = MagicMock()
        vendor.key = "openai"
        vendor.audit_enabled = False
        vendor.audit_capture_bodies = False
        vendor.id = 1

        first_endpoint = MagicMock()
        first_endpoint.base_url = "https://first.example.com/v1"
        second_endpoint = MagicMock()
        second_endpoint.base_url = "https://second.example.com/v1"

        first_connection = MagicMock()
        first_connection.id = 1001
        first_connection.endpoint_id = 501
        first_connection.endpoint_rel = first_endpoint
        first_connection.pricing_template_rel = None
        first_connection.name = "first"
        first_connection.custom_headers = None
        first_connection.auth_type = None
        first_connection.max_in_flight_non_stream = 1

        second_connection = MagicMock()
        second_connection.id = 1002
        second_connection.endpoint_id = 502
        second_connection.endpoint_rel = second_endpoint
        second_connection.pricing_template_rel = None
        second_connection.name = "second"
        second_connection.custom_headers = None
        second_connection.auth_type = None
        second_connection.max_in_flight_non_stream = 1

        model_config = MagicMock()
        model_config.vendor = vendor
        model_config.api_family = "openai"
        model_config.model_id = "gpt-4o-mini"
        model_config.loadbalance_strategy = SimpleNamespace(
            strategy_type="adaptive",
            routing_policy=make_routing_policy_adaptive(),
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
                    return_value=_attempt_plan(first_connection, second_connection)
                ),
            ),
            patch(
                "app.routers.proxy._endpoint_is_active_now",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.load_costing_settings",
                AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.compute_cost_fields",
                return_value={},
            ),
            patch("app.routers.proxy.log_request", AsyncMock(return_value=901)),
            patch("app.routers.proxy.record_audit_log", AsyncMock()),
            patch(
                "app.routers.proxy.acquire_connection_limit",
                AsyncMock(
                    side_effect=[
                        LimiterAcquireResult(
                            admitted=False,
                            deny_reason="qps_limit",
                        ),
                        LimiterAcquireResult(
                            admitted=True,
                            lease_token="lease-2",
                        ),
                    ]
                ),
            ),
            patch("app.routers.proxy.release_connection_lease", AsyncMock()),
        ):
            response = await _handle_proxy(
                request=request,
                db=mock_db,
                raw_body=raw_body,
                request_path="/v1/chat/completions",
                profile_id=1,
            )

        assert response.status_code == 200
        assert app.state.http_client.sent_urls == [
            "https://second.example.com/v1/v1/chat/completions"
        ]

    @pytest.mark.asyncio
    async def test_buffered_failover_releases_limiter_lease_before_retry(self):
        from fastapi import FastAPI
        import httpx
        from starlette.requests import Request

        from app.routers.proxy import _handle_proxy
        from app.services.loadbalancer.limiter import LimiterAcquireResult

        class DummyHttpClient:
            def __init__(self):
                self._responses = [
                    httpx.Response(
                        status_code=500,
                        headers={"content-type": "application/json"},
                        content=b'{"error":{"message":"retry"}}',
                    ),
                    httpx.Response(
                        status_code=200,
                        headers={"content-type": "application/json"},
                        content=b'{"id":"ok"}',
                    ),
                ]

            def build_request(self, method: str, upstream_url: str, **kwargs):
                return httpx.Request(
                    method=method,
                    url=upstream_url,
                    headers=kwargs.get("headers"),
                    content=kwargs.get("content"),
                )

            async def send(self, request: httpx.Request, **kwargs):
                response = self._responses.pop(0)
                response.request = request
                return response

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

        vendor = MagicMock()
        vendor.key = "openai"
        vendor.audit_enabled = False
        vendor.audit_capture_bodies = False
        vendor.id = 1

        first_endpoint = MagicMock()
        first_endpoint.base_url = "https://first.example.com/v1"
        second_endpoint = MagicMock()
        second_endpoint.base_url = "https://second.example.com/v1"

        first_connection = MagicMock()
        first_connection.id = 1001
        first_connection.endpoint_id = 501
        first_connection.endpoint_rel = first_endpoint
        first_connection.pricing_template_rel = None
        first_connection.name = "first"
        first_connection.custom_headers = None
        first_connection.auth_type = None
        first_connection.max_in_flight_non_stream = 1

        second_connection = MagicMock()
        second_connection.id = 1002
        second_connection.endpoint_id = 502
        second_connection.endpoint_rel = second_endpoint
        second_connection.pricing_template_rel = None
        second_connection.name = "second"
        second_connection.custom_headers = None
        second_connection.auth_type = None
        second_connection.max_in_flight_non_stream = 1

        model_config = MagicMock()
        model_config.vendor = vendor
        model_config.api_family = "openai"
        model_config.model_id = "gpt-4o-mini"
        model_config.loadbalance_strategy = SimpleNamespace(
            strategy_type="adaptive",
            routing_policy=make_routing_policy_adaptive(base_open_seconds=17),
        )

        mock_rules_result = MagicMock()
        mock_rules_result.scalars.return_value.all.return_value = []
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_rules_result)
        release_connection_lease = AsyncMock()

        with (
            patch(
                "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                AsyncMock(return_value=model_config),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.build_attempt_plan",
                AsyncMock(
                    return_value=_attempt_plan(first_connection, second_connection)
                ),
            ),
            patch(
                "app.routers.proxy._endpoint_is_active_now",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.load_costing_settings",
                AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.compute_cost_fields",
                return_value={},
            ),
            patch("app.routers.proxy.log_request", AsyncMock(side_effect=[901, 902])),
            patch("app.routers.proxy.record_audit_log", AsyncMock()),
            patch("app.routers.proxy.record_connection_failure", AsyncMock()),
            patch("app.routers.proxy.record_connection_recovery", AsyncMock()),
            patch(
                "app.routers.proxy.acquire_connection_limit",
                AsyncMock(
                    side_effect=[
                        LimiterAcquireResult(admitted=True, lease_token="lease-1"),
                        LimiterAcquireResult(admitted=True, lease_token="lease-2"),
                    ]
                ),
            ),
            patch(
                "app.routers.proxy.release_connection_lease",
                release_connection_lease,
            ),
        ):
            response = await _handle_proxy(
                request=request,
                db=mock_db,
                raw_body=raw_body,
                request_path="/v1/chat/completions",
                profile_id=1,
            )

        assert response.status_code == 200
        assert [
            call.kwargs["lease_token"]
            for call in release_connection_lease.await_args_list
        ] == ["lease-1", "lease-2"]

    @pytest.mark.asyncio
    async def test_transport_exception_releases_limiter_lease_before_retry(self):
        from fastapi import FastAPI
        import httpx
        from starlette.requests import Request

        from app.routers.proxy import _handle_proxy
        from app.services.loadbalancer.limiter import LimiterAcquireResult

        class DummyHttpClient:
            def __init__(self):
                self.calls = 0

            def build_request(self, method: str, upstream_url: str, **kwargs):
                return httpx.Request(
                    method=method,
                    url=upstream_url,
                    headers=kwargs.get("headers"),
                    content=kwargs.get("content"),
                )

            async def send(self, request: httpx.Request, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise httpx.ConnectError("connect fail")
                return httpx.Response(
                    status_code=200,
                    request=request,
                    headers={"content-type": "application/json"},
                    content=b'{"id":"ok"}',
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

        vendor = MagicMock()
        vendor.key = "openai"
        vendor.audit_enabled = False
        vendor.audit_capture_bodies = False
        vendor.id = 1

        first_endpoint = MagicMock()
        first_endpoint.base_url = "https://first.example.com/v1"
        second_endpoint = MagicMock()
        second_endpoint.base_url = "https://second.example.com/v1"

        first_connection = MagicMock()
        first_connection.id = 1001
        first_connection.endpoint_id = 501
        first_connection.endpoint_rel = first_endpoint
        first_connection.pricing_template_rel = None
        first_connection.name = "first"
        first_connection.custom_headers = None
        first_connection.auth_type = None
        first_connection.max_in_flight_non_stream = 1

        second_connection = MagicMock()
        second_connection.id = 1002
        second_connection.endpoint_id = 502
        second_connection.endpoint_rel = second_endpoint
        second_connection.pricing_template_rel = None
        second_connection.name = "second"
        second_connection.custom_headers = None
        second_connection.auth_type = None
        second_connection.max_in_flight_non_stream = 1

        model_config = MagicMock()
        model_config.vendor = vendor
        model_config.api_family = "openai"
        model_config.model_id = "gpt-4o-mini"
        model_config.loadbalance_strategy = SimpleNamespace(
            strategy_type="adaptive",
            routing_policy=make_routing_policy_adaptive(),
        )

        mock_rules_result = MagicMock()
        mock_rules_result.scalars.return_value.all.return_value = []
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_rules_result)
        release_connection_lease = AsyncMock()

        with (
            patch(
                "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                AsyncMock(return_value=model_config),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.build_attempt_plan",
                AsyncMock(
                    return_value=_attempt_plan(first_connection, second_connection)
                ),
            ),
            patch(
                "app.routers.proxy._endpoint_is_active_now",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.load_costing_settings",
                AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.compute_cost_fields",
                return_value={},
            ),
            patch("app.routers.proxy.log_request", AsyncMock(side_effect=[901, 902])),
            patch("app.routers.proxy.record_audit_log", AsyncMock()),
            patch("app.routers.proxy.record_connection_failure", AsyncMock()),
            patch("app.routers.proxy.record_connection_recovery", AsyncMock()),
            patch(
                "app.routers.proxy.acquire_connection_limit",
                AsyncMock(
                    side_effect=[
                        LimiterAcquireResult(admitted=True, lease_token="lease-1"),
                        LimiterAcquireResult(admitted=True, lease_token="lease-2"),
                    ]
                ),
            ),
            patch(
                "app.routers.proxy.release_connection_lease",
                release_connection_lease,
            ),
        ):
            response = await _handle_proxy(
                request=request,
                db=mock_db,
                raw_body=raw_body,
                request_path="/v1/chat/completions",
                profile_id=1,
            )

        assert response.status_code == 200
        assert [
            call.kwargs["lease_token"]
            for call in release_connection_lease.await_args_list
        ] == ["lease-1", "lease-2"]

    @pytest.mark.asyncio
    async def test_streaming_read_error_fails_over_to_next_connection(self):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from starlette.requests import Request

        from app.routers.proxy import _handle_proxy
        from app.services.loadbalancer.limiter import LimiterAcquireResult

        class SuccessStreamResponse:
            def __init__(self):
                self.status_code = 200
                self.headers = {"content-type": "text/event-stream"}
                self.closed = False

            async def aiter_bytes(self):
                yield b'data: {"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
                yield b"data: [DONE]\n\n"

            async def aclose(self):
                self.closed = True

        class DummyHttpClient:
            def __init__(self):
                self.calls = 0
                self.sent_urls: list[str] = []
                self.success_response = SuccessStreamResponse()

            def build_request(self, method: str, upstream_url: str, **kwargs):
                return httpx.Request(
                    method=method,
                    url=upstream_url,
                    headers=kwargs.get("headers"),
                    content=kwargs.get("content"),
                )

            async def send(self, request: httpx.Request, **kwargs):
                assert kwargs.get("stream") is True
                self.calls += 1
                self.sent_urls.append(str(request.url))
                if self.calls == 1:
                    raise httpx.ReadError("read fail")
                return self.success_response

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
            {
                "model": "gpt-4o-mini",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            }
        ).encode("utf-8")

        vendor = MagicMock()
        vendor.key = "openai"
        vendor.audit_enabled = False
        vendor.audit_capture_bodies = False
        vendor.id = 1

        first_endpoint = MagicMock()
        first_endpoint.base_url = "https://first.example.com/v1"
        second_endpoint = MagicMock()
        second_endpoint.base_url = "https://second.example.com/v1"

        first_connection = MagicMock()
        first_connection.id = 1001
        first_connection.endpoint_id = 501
        first_connection.endpoint_rel = first_endpoint
        first_connection.pricing_template_rel = None
        first_connection.name = "first"
        first_connection.custom_headers = None
        first_connection.auth_type = None
        first_connection.max_in_flight_stream = 1

        second_connection = MagicMock()
        second_connection.id = 1002
        second_connection.endpoint_id = 502
        second_connection.endpoint_rel = second_endpoint
        second_connection.pricing_template_rel = None
        second_connection.name = "second"
        second_connection.custom_headers = None
        second_connection.auth_type = None
        second_connection.max_in_flight_stream = 1

        model_config = MagicMock()
        model_config.vendor = vendor
        model_config.api_family = "openai"
        model_config.model_id = "gpt-4o-mini"
        model_config.loadbalance_strategy = SimpleNamespace(
            strategy_type="adaptive",
            routing_policy=make_routing_policy_adaptive(),
        )

        mock_rules_result = MagicMock()
        mock_rules_result.scalars.return_value.all.return_value = []
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_rules_result)
        log_request = AsyncMock(side_effect=[901, 902])
        manager = BackgroundTaskManager()
        await manager.start()

        try:
            with (
                patch(
                    "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                    AsyncMock(return_value=model_config),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.build_attempt_plan",
                    AsyncMock(
                        return_value=_attempt_plan(first_connection, second_connection)
                    ),
                ),
                patch(
                    "app.routers.proxy._endpoint_is_active_now",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.load_costing_settings",
                    AsyncMock(return_value=MagicMock()),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.compute_cost_fields",
                    return_value={},
                ),
                patch("app.routers.proxy.log_request", log_request),
                patch("app.routers.proxy.record_audit_log", AsyncMock()),
                patch("app.routers.proxy.record_connection_failure", AsyncMock()),
                patch("app.routers.proxy.record_connection_recovery", AsyncMock()),
                patch(
                    "app.routers.proxy.acquire_connection_limit",
                    AsyncMock(
                        side_effect=[
                            LimiterAcquireResult(admitted=True, lease_token="lease-1"),
                            LimiterAcquireResult(admitted=True, lease_token="lease-2"),
                        ]
                    ),
                ),
                patch("app.routers.proxy.release_connection_lease", AsyncMock()),
                patch(
                    "app.routers.proxy_domains.attempt_outcome_reporting.background_task_manager",
                    manager,
                ),
            ):
                response = await _handle_proxy(
                    request=request,
                    db=mock_db,
                    raw_body=raw_body,
                    request_path="/v1/chat/completions",
                    profile_id=1,
                )

                assert response.status_code == 200
                assert isinstance(response, StreamingResponse)

                stream = cast(AsyncGenerator[bytes, None], response.body_iterator)
                streamed_chunks = [chunk async for chunk in stream]

            assert streamed_chunks == [
                b'data: {"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n',
                b"data: [DONE]\n\n",
            ]
            assert app.state.http_client.sent_urls == [
                "https://first.example.com/v1/v1/chat/completions",
                "https://second.example.com/v1/v1/chat/completions",
            ]
            assert app.state.http_client.success_response.closed is True
            assert log_request.await_count == 2
            first_call = log_request.await_args_list[0].kwargs
            second_call = log_request.await_args_list[1].kwargs
            assert first_call["attempt_number"] == 1
            assert second_call["attempt_number"] == 2
            assert first_call["ingress_request_id"] == second_call["ingress_request_id"]
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_streaming_read_error_releases_limiter_lease_and_records_failure_before_retry(
        self,
    ):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from starlette.requests import Request

        from app.routers.proxy import _handle_proxy
        from app.services.loadbalancer.limiter import LimiterAcquireResult

        class SuccessStreamResponse:
            def __init__(self):
                self.status_code = 200
                self.headers = {"content-type": "text/event-stream"}
                self.closed = False

            async def aiter_bytes(self):
                yield b'data: {"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
                yield b"data: [DONE]\n\n"

            async def aclose(self):
                self.closed = True

        event_log: list[str] = []

        class DummyHttpClient:
            def __init__(self):
                self.calls = 0
                self.success_response = SuccessStreamResponse()

            def build_request(self, method: str, upstream_url: str, **kwargs):
                return httpx.Request(
                    method=method,
                    url=upstream_url,
                    headers=kwargs.get("headers"),
                    content=kwargs.get("content"),
                )

            async def send(self, request: httpx.Request, **kwargs):
                assert kwargs.get("stream") is True
                self.calls += 1
                if self.calls == 1:
                    event_log.append("send:first")
                    raise httpx.ReadError("read fail")
                event_log.append("send:second")
                return self.success_response

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
            {
                "model": "gpt-4o-mini",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            }
        ).encode("utf-8")

        vendor = MagicMock()
        vendor.key = "openai"
        vendor.audit_enabled = False
        vendor.audit_capture_bodies = False
        vendor.id = 1

        first_endpoint = MagicMock()
        first_endpoint.base_url = "https://first.example.com/v1"
        second_endpoint = MagicMock()
        second_endpoint.base_url = "https://second.example.com/v1"

        first_connection = MagicMock()
        first_connection.id = 1001
        first_connection.endpoint_id = 501
        first_connection.endpoint_rel = first_endpoint
        first_connection.pricing_template_rel = None
        first_connection.name = "first"
        first_connection.custom_headers = None
        first_connection.auth_type = None
        first_connection.max_in_flight_stream = 1

        second_connection = MagicMock()
        second_connection.id = 1002
        second_connection.endpoint_id = 502
        second_connection.endpoint_rel = second_endpoint
        second_connection.pricing_template_rel = None
        second_connection.name = "second"
        second_connection.custom_headers = None
        second_connection.auth_type = None
        second_connection.max_in_flight_stream = 1

        model_config = MagicMock()
        model_config.vendor = vendor
        model_config.api_family = "openai"
        model_config.model_id = "gpt-4o-mini"
        model_config.loadbalance_strategy = SimpleNamespace(
            strategy_type="adaptive",
            routing_policy=make_routing_policy_adaptive(base_open_seconds=17),
        )

        mock_rules_result = MagicMock()
        mock_rules_result.scalars.return_value.all.return_value = []
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_rules_result)
        release_connection_lease = AsyncMock()
        record_connection_failure = AsyncMock()
        record_connection_recovery = AsyncMock()
        manager = BackgroundTaskManager()
        await manager.start()

        async def release_lease(*args, **kwargs):
            event_log.append(f"release:{kwargs['lease_token']}")
            return True

        async def mark_failure(*args, **kwargs):
            event_log.append(f"failure:{args[1]}")

        async def mark_recovery(*args, **kwargs):
            event_log.append(f"recovery:{args[1]}")

        release_connection_lease.side_effect = release_lease
        record_connection_failure.side_effect = mark_failure
        record_connection_recovery.side_effect = mark_recovery

        try:
            with (
                patch(
                    "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                    AsyncMock(return_value=model_config),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.build_attempt_plan",
                    AsyncMock(
                        return_value=_attempt_plan(
                            first_connection,
                            second_connection,
                            policy=make_routing_policy_adaptive(base_open_seconds=17),
                        )
                    ),
                ),
                patch(
                    "app.routers.proxy._endpoint_is_active_now",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.load_costing_settings",
                    AsyncMock(return_value=MagicMock()),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.compute_cost_fields",
                    return_value={},
                ),
                patch(
                    "app.routers.proxy.log_request", AsyncMock(side_effect=[901, 902])
                ),
                patch("app.routers.proxy.record_audit_log", AsyncMock()),
                patch(
                    "app.routers.proxy.acquire_connection_limit",
                    AsyncMock(
                        side_effect=[
                            LimiterAcquireResult(admitted=True, lease_token="lease-1"),
                            LimiterAcquireResult(admitted=True, lease_token="lease-2"),
                        ]
                    ),
                ),
                patch(
                    "app.routers.proxy.release_connection_lease",
                    release_connection_lease,
                ),
                patch(
                    "app.routers.proxy.record_connection_failure",
                    record_connection_failure,
                ),
                patch(
                    "app.routers.proxy.record_connection_recovery",
                    record_connection_recovery,
                ),
                patch(
                    "app.routers.proxy_domains.attempt_outcome_reporting.background_task_manager",
                    manager,
                ),
            ):
                response = await _handle_proxy(
                    request=request,
                    db=mock_db,
                    raw_body=raw_body,
                    request_path="/v1/chat/completions",
                    profile_id=1,
                )

                assert response.status_code == 200
                assert isinstance(response, StreamingResponse)
                stream = cast(AsyncGenerator[bytes, None], response.body_iterator)
                async for _ in stream:
                    pass

            release_tokens = [
                call.kwargs["lease_token"]
                for call in release_connection_lease.await_args_list
            ]
            assert release_tokens == ["lease-1", "lease-2"]

            failure_call = record_connection_failure.await_args
            assert failure_call is not None
            assert failure_call.args[1] == 1001
            assert failure_call.args[2] == 17
            assert failure_call.args[3] == "connect_error"
            assert failure_call.args[6] == 501

            recovery_calls = record_connection_recovery.await_args_list
            assert recovery_calls
            assert all(call.args[1] != 1001 for call in recovery_calls)

            assert event_log.index("release:lease-1") < event_log.index("send:second")
            assert event_log.index("failure:1001") < event_log.index("send:second")
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_handle_transport_exception_labels_read_error_without_timeout_wording(
        self,
    ):
        import httpx

        from app.routers.proxy_domains.attempt_handlers import (
            handle_transport_exception,
        )
        from app.routers.proxy_domains.attempt_types import (
            ProxyAttemptTarget,
            ProxyRequestState,
            ProxyRuntimeDependencies,
        )

        release_connection_lease = AsyncMock(return_value=True)
        record_connection_failure = AsyncMock()

        deps = cast(
            ProxyRuntimeDependencies,
            cast(
                object,
                SimpleNamespace(
                    release_connection_lease_fn=release_connection_lease,
                    record_connection_failure_fn=record_connection_failure,
                ),
            ),
        )
        state = cast(
            ProxyRequestState,
            cast(
                object,
                SimpleNamespace(
                    profile_id=7,
                    setup=SimpleNamespace(
                        is_streaming=True,
                        failover_policy=SimpleNamespace(
                            failover_recovery_enabled=True,
                            failover_cooldown_seconds=17,
                        ),
                        model_id="gpt-4o-mini",
                        vendor_id=1,
                    ),
                ),
            ),
        )
        target = cast(
            ProxyAttemptTarget,
            cast(
                object,
                SimpleNamespace(
                    connection=SimpleNamespace(id=1001, endpoint_id=501),
                    limiter_lease_token="lease-1",
                ),
            ),
        )

        with patch(
            "app.routers.proxy_domains.attempt_handlers.log_and_audit_attempt",
            AsyncMock(),
        ):
            result = await handle_transport_exception(
                deps=deps,
                state=state,
                target=target,
                start_time=0.0,
                exc=httpx.ReadError("read fail"),
            )

        assert result.error_detail is not None
        assert result.error_detail.startswith("Read error:")
        assert "Timeout" not in result.error_detail
        release_connection_lease.assert_awaited_once_with(
            profile_id=7,
            lease_token="lease-1",
            now_at=None,
        )
        record_connection_failure.assert_awaited_once()


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
    async def test_proxy_skips_endpoint_disabled_after_plan_uses_next_endpoint_and_preserves_chat_completion_payload(
        self,
    ):
        import httpx
        from fastapi import FastAPI
        from sqlalchemy import select, update
        from starlette.requests import Request

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import (
            Connection,
            Endpoint,
            LoadbalanceStrategy,
            ModelConfig,
            Profile,
            Vendor,
        )
        from app.routers.proxy import _handle_proxy
        from app.services.loadbalancer.planner import (
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
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": "ALPHA",
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
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
                vendor = Vendor(
                    key=f"def012-openai-{uuid4().hex[:8]}",
                    name=f"OpenAI DEF012 {uuid4().hex[:8]}",
                    audit_enabled=False,
                    audit_capture_bodies=False,
                )
                model = ModelConfig(
                    vendor=vendor,
                    api_family="openai",
                    profile=profile,
                    model_id="gpt-4o-mini-def012",
                    display_name="GPT-4o Mini DEF012",
                    model_type="native",
                    loadbalance_strategy=LoadbalanceStrategy(
                        profile=profile,
                        name=f"def012-strategy-{uuid4().hex[:8]}",
                        routing_policy=make_routing_policy_adaptive(),
                    ),
                    is_enabled=True,
                )
                primary_endpoint = Endpoint(
                    name="primary",
                    profile=profile,
                    base_url="https://primary.example.com/v1",
                    api_key="sk-primary",
                    position=0,
                )
                secondary_endpoint = Endpoint(
                    name="secondary",
                    profile=profile,
                    base_url="https://secondary.example.com/v1",
                    api_key="sk-secondary",
                    position=1,
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
                        vendor,
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

                async def build_plan_with_assert(
                    current_db,
                    profile_id,
                    model_config,
                    now_at,
                    *,
                    is_streaming=False,
                ):
                    plan = await real_build_attempt_plan(
                        current_db,
                        profile_id,
                        model_config,
                        now_at,
                        is_streaming=is_streaming,
                    )
                    assert [ep.id for ep in plan.connections] == [
                        primary_id,
                        secondary_id,
                    ]
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
                        "app.routers.proxy_domains.request_setup.build_attempt_plan",
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
                payload = json.loads(response.body.decode("utf-8"))

                assert payload["id"] == "chatcmpl-ok"
                assert payload["choices"][0]["message"]["content"] == "ALPHA"
                assert payload["usage"]["total_tokens"] == 2
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
            pass


class TestFillFirstRuntimeBehavior:
    @staticmethod
    def _build_request(app, raw_body: bytes):
        from starlette.requests import Request

        return Request(
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

    @pytest.mark.asyncio
    async def test_fill_first_runtime_packs_requests_on_highest_priority_then_spills_on_limiter_denial(
        self,
    ):
        import httpx
        from fastapi import FastAPI

        from app.routers.proxy import _handle_proxy
        from app.services.loadbalancer.limiter import LimiterAcquireResult

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
                    content=b'{"id":"ok"}',
                )

        app = FastAPI()
        app.state.http_client = DummyHttpClient()
        raw_body = json.dumps(
            {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
        ).encode("utf-8")

        vendor = SimpleNamespace(
            key="openai",
            audit_enabled=False,
            audit_capture_bodies=False,
            id=1,
        )
        primary_endpoint = SimpleNamespace(
            id=501,
            name="primary",
            base_url="https://primary.example.com/v1",
            api_key="sk-primary",
        )
        secondary_endpoint = SimpleNamespace(
            id=502,
            name="secondary",
            base_url="https://secondary.example.com/v1",
            api_key="sk-secondary",
        )
        primary_connection = SimpleNamespace(
            id=1001,
            endpoint_id=501,
            endpoint_rel=primary_endpoint,
            pricing_template_rel=None,
            name="primary",
            custom_headers=None,
            auth_type=None,
            qps_limit=3,
            max_in_flight_non_stream=None,
            max_in_flight_stream=None,
            priority=0,
            health_status="unhealthy",
            is_active=True,
        )
        secondary_connection = SimpleNamespace(
            id=1002,
            endpoint_id=502,
            endpoint_rel=secondary_endpoint,
            pricing_template_rel=None,
            name="secondary",
            custom_headers=None,
            auth_type=None,
            qps_limit=None,
            max_in_flight_non_stream=None,
            max_in_flight_stream=None,
            priority=1,
            health_status="healthy",
            is_active=True,
        )
        model_config = SimpleNamespace(
            vendor=vendor,
            api_family="openai",
            model_id="gpt-4o-mini",
            loadbalance_strategy=SimpleNamespace(
                strategy_type="fill-first",
                failover_recovery_enabled=False,
            ),
            connections=[secondary_connection, primary_connection],
        )

        mock_rules_result = MagicMock()
        mock_rules_result.scalars.return_value.all.return_value = []
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_rules_result)

        primary_attempts = 0

        async def acquire_limit(
            *, profile_id, connection, lease_kind, lease_ttl_seconds, now_at
        ):
            nonlocal primary_attempts
            if connection.id == primary_connection.id:
                primary_attempts += 1
                if primary_attempts <= 3:
                    return LimiterAcquireResult(
                        admitted=True,
                        lease_token=f"primary-{primary_attempts}",
                    )
                return LimiterAcquireResult(
                    admitted=False,
                    deny_reason="qps_limit",
                )
            return LimiterAcquireResult(
                admitted=True,
                lease_token="secondary-1",
            )

        async def run_request():
            request = self._build_request(app, raw_body)
            return await _handle_proxy(
                request=request,
                db=mock_db,
                raw_body=raw_body,
                request_path="/v1/chat/completions",
                profile_id=1,
            )

        with (
            patch(
                "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                AsyncMock(return_value=model_config),
            ),
            patch(
                "app.routers.proxy._endpoint_is_active_now",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.load_costing_settings",
                AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.compute_cost_fields",
                return_value={},
            ),
            patch(
                "app.routers.proxy.acquire_connection_limit",
                AsyncMock(side_effect=acquire_limit),
            ),
            patch("app.routers.proxy.release_connection_lease", AsyncMock()),
            patch("app.routers.proxy.log_request", AsyncMock(return_value=901)),
            patch("app.routers.proxy.record_audit_log", AsyncMock()),
            patch("app.routers.proxy.record_connection_failure", AsyncMock()),
            patch("app.routers.proxy.record_connection_recovery", AsyncMock()),
        ):
            responses = await asyncio.gather(*(run_request() for _ in range(4)))

        assert [response.status_code for response in responses] == [200, 200, 200, 200]
        assert (
            app.state.http_client.sent_urls.count(
                "https://primary.example.com/v1/v1/chat/completions"
            )
            == 3
        )
        assert (
            app.state.http_client.sent_urls.count(
                "https://secondary.example.com/v1/v1/chat/completions"
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_fill_first_runtime_skips_disabled_highest_priority_connection(
        self,
    ):
        import httpx
        from fastapi import FastAPI

        from app.routers.proxy import _handle_proxy

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
                    content=b'{"id":"ok"}',
                )

        app = FastAPI()
        app.state.http_client = DummyHttpClient()
        raw_body = json.dumps(
            {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
        ).encode("utf-8")

        vendor = SimpleNamespace(
            key="openai",
            audit_enabled=False,
            audit_capture_bodies=False,
            id=1,
        )
        primary_endpoint = SimpleNamespace(
            id=501,
            name="primary",
            base_url="https://primary.example.com/v1",
            api_key="sk-primary",
        )
        secondary_endpoint = SimpleNamespace(
            id=502,
            name="secondary",
            base_url="https://secondary.example.com/v1",
            api_key="sk-secondary",
        )
        primary_connection = SimpleNamespace(
            id=1001,
            endpoint_id=501,
            endpoint_rel=primary_endpoint,
            pricing_template_rel=None,
            name="primary",
            custom_headers=None,
            auth_type=None,
            qps_limit=None,
            max_in_flight_non_stream=None,
            max_in_flight_stream=None,
            priority=0,
            health_status="healthy",
            is_active=True,
        )
        secondary_connection = SimpleNamespace(
            id=1002,
            endpoint_id=502,
            endpoint_rel=secondary_endpoint,
            pricing_template_rel=None,
            name="secondary",
            custom_headers=None,
            auth_type=None,
            qps_limit=None,
            max_in_flight_non_stream=None,
            max_in_flight_stream=None,
            priority=1,
            health_status="healthy",
            is_active=True,
        )
        model_config = SimpleNamespace(
            vendor=vendor,
            api_family="openai",
            model_id="gpt-4o-mini",
            loadbalance_strategy=SimpleNamespace(
                strategy_type="fill-first",
                failover_recovery_enabled=False,
            ),
            connections=[secondary_connection, primary_connection],
        )

        mock_rules_result = MagicMock()
        mock_rules_result.scalars.return_value.all.return_value = []
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_rules_result)

        async def endpoint_is_active_now(db, connection_id):
            return connection_id != primary_connection.id

        with (
            patch(
                "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                AsyncMock(return_value=model_config),
            ),
            patch(
                "app.routers.proxy._endpoint_is_active_now",
                AsyncMock(side_effect=endpoint_is_active_now),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.load_costing_settings",
                AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.compute_cost_fields",
                return_value={},
            ),
            patch("app.routers.proxy.log_request", AsyncMock(return_value=901)),
            patch("app.routers.proxy.record_audit_log", AsyncMock()),
            patch("app.routers.proxy.record_connection_failure", AsyncMock()),
            patch("app.routers.proxy.record_connection_recovery", AsyncMock()),
        ):
            response = await _handle_proxy(
                request=self._build_request(app, raw_body),
                db=mock_db,
                raw_body=raw_body,
                request_path="/v1/chat/completions",
                profile_id=1,
            )

        assert response.status_code == 200
        assert app.state.http_client.sent_urls == [
            "https://secondary.example.com/v1/v1/chat/completions"
        ]


class TestDEF021_StreamingCancellationResilience:
    @staticmethod
    def _build_request(
        app,
        raw_body: bytes,
        *,
        path: str = "/v1/responses",
        raw_path: bytes = b"/v1/responses",
    ):
        from starlette.requests import Request

        if raw_path == b"/v1/responses" and path != "/v1/responses":
            raw_path = path.encode("utf-8")

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
                "path": path,
                "raw_path": raw_path,
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
    def _build_model_config_and_endpoint(
        *,
        api_family: str = "openai",
        model_id: str = "gpt-4o-mini",
    ):
        vendor = MagicMock()
        vendor.key = "google" if api_family == "gemini" else api_family
        vendor.audit_enabled = True
        vendor.audit_capture_bodies = False
        vendor.id = 11

        endpoint = MagicMock()
        endpoint.id = 201
        endpoint.endpoint_id = 201
        endpoint.base_url = "https://api.openai.com"
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
        model_config.vendor = vendor
        model_config.api_family = api_family
        model_config.model_id = model_id
        model_config.loadbalance_strategy = _legacy_single_strategy()

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
        model_config.vendor.audit_capture_bodies = True
        log_started = asyncio.Event()
        release_log = asyncio.Event()

        async def delayed_log_request(*args, **kwargs):
            log_started.set()
            await release_log.wait()
            return 501

        try:

            def build_cost_fields_for_assertion(**kwargs):
                return {
                    "cache_read_input_tokens": kwargs["cache_read_input_tokens"],
                    "cache_creation_input_tokens": kwargs[
                        "cache_creation_input_tokens"
                    ],
                    "reasoning_tokens": kwargs["reasoning_tokens"],
                }

            with (
                patch(
                    "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                    AsyncMock(return_value=model_config),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.build_attempt_plan",
                    return_value=_attempt_plan(endpoint),
                ),
                patch(
                    "app.routers.proxy._endpoint_is_active_now",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.load_costing_settings",
                    AsyncMock(return_value=MagicMock()),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.compute_cost_fields",
                    return_value={},
                ),
                patch(
                    "app.routers.proxy.log_request",
                    AsyncMock(side_effect=delayed_log_request),
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

                next_task = asyncio.create_task(stream.__anext__())
                await asyncio.wait_for(log_started.wait(), timeout=1)
                assert audit_mock.await_count == 0
                release_log.set()

                with pytest.raises(asyncio.CancelledError):
                    await next_task

                await self._wait_for_asyncmock_calls(log_mock)
                await self._wait_for_asyncmock_calls(audit_mock)

                assert upstream_resp.closed is True
                log_mock.assert_awaited_once()
                audit_mock.assert_awaited_once()
                audit_call = audit_mock.await_args
                assert audit_call is not None
                assert audit_call.kwargs["request_log_id"] == 501
                assert audit_call.kwargs["response_body"] == first
                assert audit_call.kwargs["capture_bodies"] is True
                assert "Failed to log streaming request" not in caplog.text
                assert "Failed to log streaming audit" not in caplog.text
        finally:
            pass


class TestDEF080_OpenAIChatStreamingUsageOptIn:
    @staticmethod
    def _build_request(
        app,
        raw_body: bytes,
        *,
        path: str = "/v1/responses",
        raw_path: bytes | None = None,
    ):
        return TestDEF021_StreamingCancellationResilience._build_request(
            app,
            raw_body,
            path=path,
            raw_path=path.encode("utf-8") if raw_path is None else raw_path,
        )

    @staticmethod
    def _build_db_mock():
        return TestDEF021_StreamingCancellationResilience._build_db_mock()

    @staticmethod
    def _build_model_config_and_endpoint(**kwargs):
        return (
            TestDEF021_StreamingCancellationResilience._build_model_config_and_endpoint(
                **kwargs,
            )
        )

    @staticmethod
    async def _wait_for_asyncmock_calls(
        mock_obj: AsyncMock, expected_min_calls: int = 1
    ):
        await TestDEF021_StreamingCancellationResilience._wait_for_asyncmock_calls(
            mock_obj,
            expected_min_calls=expected_min_calls,
        )

    @pytest.mark.asyncio
    async def test_prepare_proxy_request_injects_include_usage_for_openai_chat_streams(
        self,
    ):
        from fastapi import FastAPI
        from app.routers.proxy_domains.request_setup import prepare_proxy_request

        app = FastAPI()
        app.state.http_client = object()

        raw_body = json.dumps(
            {
                "model": "gpt-4o-mini",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            }
        ).encode("utf-8")
        request = TestDEF021_StreamingCancellationResilience._build_request(
            app,
            raw_body,
            path="/v1/chat/completions",
        )

        vendor = SimpleNamespace(
            key="anthropic",
            audit_enabled=False,
            audit_capture_bodies=False,
            id=1,
        )
        vendor = SimpleNamespace(
            id=1,
            audit_enabled=False,
            audit_capture_bodies=False,
        )
        strategy = _legacy_single_strategy()
        connection = SimpleNamespace(endpoint_id=501)
        model_config = SimpleNamespace(
            vendor=vendor,
            api_family="openai",
            model_id="gpt-4o-mini",
            loadbalance_strategy=strategy,
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
                AsyncMock(return_value=_attempt_plan(connection)),
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
                request_path="/v1/chat/completions",
                profile_id=1,
            )

        assert setup.rewritten_body is not None
        assert setup.api_family == "openai"
        parsed = json.loads(setup.rewritten_body)
        assert parsed["stream_options"]["include_usage"] is True

    @pytest.mark.asyncio
    async def test_prepare_proxy_request_does_not_inject_include_usage_for_responses_streams(
        self,
    ):
        from fastapi import FastAPI
        from app.routers.proxy_domains.request_setup import prepare_proxy_request

        app = FastAPI()
        app.state.http_client = object()

        raw_body = json.dumps(
            {
                "model": "gpt-4o-mini",
                "stream": True,
                "input": "hi",
            }
        ).encode("utf-8")
        request = TestDEF021_StreamingCancellationResilience._build_request(
            app,
            raw_body,
            path="/v1/responses",
        )

        vendor = SimpleNamespace(
            key="anthropic",
            audit_enabled=False,
            audit_capture_bodies=False,
            id=1,
        )
        vendor = SimpleNamespace(
            id=1,
            audit_enabled=False,
            audit_capture_bodies=False,
        )
        strategy = _legacy_single_strategy()
        connection = SimpleNamespace(endpoint_id=501)
        model_config = SimpleNamespace(
            vendor=vendor,
            api_family="openai",
            model_id="gpt-4o-mini",
            loadbalance_strategy=strategy,
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
                AsyncMock(return_value=_attempt_plan(connection)),
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
                request_path="/v1/responses",
                profile_id=1,
            )

        assert setup.rewritten_body == raw_body

    @pytest.mark.asyncio
    async def test_chat_completions_stream_logs_usage_without_body_capture_when_proxy_requests_it(
        self, caplog
    ):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from app.routers.proxy import _handle_proxy

        caplog.set_level(logging.ERROR)

        delta_chunk = b'data: {"id":"chatcmpl-123","choices":[{"index":0,"delta":{"content":"Hello"}}],"usage":null}\n\n'
        usage_chunk = b'data: {"id":"chatcmpl-123","choices":[],"usage":{"prompt_tokens":12,"completion_tokens":7,"total_tokens":19}}\n\n'
        done_chunk = b"data: [DONE]\n\n"

        class ChatCompletionsStreamResponse:
            def __init__(self, chunks: list[bytes]):
                self.status_code = 200
                self.headers = {"content-type": "text/event-stream"}
                self.closed = False
                self._chunks = chunks

            async def aiter_bytes(self):
                for chunk in self._chunks:
                    yield chunk

            async def aclose(self):
                self.closed = True

        class DummyHttpClient:
            def __init__(self):
                self.last_request_content = None

            def build_request(self, method: str, upstream_url: str, **kwargs):
                request = httpx.Request(
                    method=method,
                    url=upstream_url,
                    headers=kwargs.get("headers"),
                    content=kwargs.get("content"),
                )
                self.last_request_content = request.content
                return request

            async def send(self, request: httpx.Request, **kwargs):
                assert kwargs.get("stream") is True
                request_payload = json.loads(request.content.decode("utf-8"))
                include_usage = (
                    request_payload.get("stream_options", {}).get("include_usage")
                    is True
                )
                chunks = [delta_chunk]
                if include_usage:
                    chunks.append(usage_chunk)
                chunks.append(done_chunk)
                return ChatCompletionsStreamResponse(chunks)

        app = FastAPI()
        client = DummyHttpClient()
        app.state.http_client = client

        raw_body = json.dumps(
            {
                "model": "gpt-4o-mini",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            }
        ).encode("utf-8")
        request = TestDEF021_StreamingCancellationResilience._build_request(
            app,
            raw_body,
            path="/v1/chat/completions",
        )
        mock_db = TestDEF021_StreamingCancellationResilience._build_db_mock()
        model_config, endpoint = (
            TestDEF021_StreamingCancellationResilience._build_model_config_and_endpoint()
        )
        model_config.vendor.audit_capture_bodies = False
        manager = BackgroundTaskManager()
        await manager.start()

        try:
            with (
                patch(
                    "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                    AsyncMock(return_value=model_config),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.build_attempt_plan",
                    return_value=_attempt_plan(endpoint),
                ),
                patch(
                    "app.routers.proxy._endpoint_is_active_now",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.load_costing_settings",
                    AsyncMock(return_value=MagicMock()),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.compute_cost_fields",
                    return_value={},
                ),
                patch(
                    "app.routers.proxy.log_request", AsyncMock(return_value=812)
                ) as log_mock,
                patch("app.routers.proxy.record_audit_log", AsyncMock()) as audit_mock,
                patch(
                    "app.routers.proxy_domains.attempt_outcome_reporting.background_task_manager",
                    manager,
                ),
            ):
                response = await _handle_proxy(
                    request=request,
                    db=mock_db,
                    raw_body=raw_body,
                    request_path="/v1/chat/completions",
                    profile_id=1,
                )

                assert response.status_code == 200
                assert isinstance(response, StreamingResponse)

                stream = cast(AsyncGenerator[bytes, None], response.body_iterator)
                received = [chunk async for chunk in stream]

                await TestDEF021_StreamingCancellationResilience._wait_for_asyncmock_calls(
                    log_mock
                )
                await TestDEF021_StreamingCancellationResilience._wait_for_asyncmock_calls(
                    audit_mock
                )

                assert b"".join(received) == delta_chunk + usage_chunk + done_chunk
                assert client.last_request_content is not None
                sent_payload = json.loads(client.last_request_content.decode("utf-8"))
                assert sent_payload["stream_options"]["include_usage"] is True

                log_mock.assert_awaited_once()
                log_call = log_mock.await_args
                assert log_call is not None
                assert log_call.kwargs["input_tokens"] == 12
                assert log_call.kwargs["output_tokens"] == 7
                assert log_call.kwargs["total_tokens"] == 19

                audit_mock.assert_awaited_once()
                audit_call = audit_mock.await_args
                assert audit_call is not None
                assert audit_call.kwargs["capture_bodies"] is False
                assert audit_call.kwargs["response_body"] is None
                assert "Failed to log streaming request" not in caplog.text
                assert "Failed to log streaming audit" not in caplog.text
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_mid_stream_cancel_without_body_capture_still_logs_tokens(
        self, caplog
    ):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from app.routers.proxy import _handle_proxy

        caplog.set_level(logging.ERROR)

        first_chunk = b'data: {"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'

        class CancelMidStreamResponse:
            def __init__(self):
                self.status_code = 200
                self.headers = {"content-type": "text/event-stream"}
                self.closed = False

            async def aiter_bytes(self):
                yield first_chunk
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
        model_config.vendor.audit_capture_bodies = False
        log_started = asyncio.Event()
        release_log = asyncio.Event()
        manager = BackgroundTaskManager()
        await manager.start()

        async def delayed_log_request(*args, **kwargs):
            log_started.set()
            await release_log.wait()
            return 591

        try:
            with (
                patch(
                    "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                    AsyncMock(return_value=model_config),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.build_attempt_plan",
                    return_value=_attempt_plan(endpoint),
                ),
                patch(
                    "app.routers.proxy._endpoint_is_active_now",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.load_costing_settings",
                    AsyncMock(return_value=MagicMock()),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.compute_cost_fields",
                    return_value={},
                ),
                patch(
                    "app.routers.proxy.log_request",
                    AsyncMock(side_effect=delayed_log_request),
                ) as log_mock,
                patch("app.routers.proxy.record_audit_log", AsyncMock()) as audit_mock,
                patch(
                    "app.routers.proxy_domains.attempt_outcome_reporting.background_task_manager",
                    manager,
                ),
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
                assert first == first_chunk

                next_task = asyncio.create_task(stream.__anext__())
                await asyncio.wait_for(log_started.wait(), timeout=1)
                assert audit_mock.await_count == 0
                release_log.set()

                with pytest.raises(asyncio.CancelledError):
                    await next_task

                await self._wait_for_asyncmock_calls(log_mock)
                await self._wait_for_asyncmock_calls(audit_mock)

                assert upstream_resp.closed is True
                log_mock.assert_awaited_once()
                log_call = log_mock.await_args
                assert log_call is not None
                assert log_call.kwargs["input_tokens"] == 1
                assert log_call.kwargs["output_tokens"] == 1
                assert log_call.kwargs["total_tokens"] == 2

                audit_mock.assert_awaited_once()
                audit_call = audit_mock.await_args
                assert audit_call is not None
                assert audit_call.kwargs["request_log_id"] == 591
                assert audit_call.kwargs["capture_bodies"] is False
                assert audit_call.kwargs["response_body"] is None
                assert "Failed to log streaming request" not in caplog.text
                assert "Failed to log streaming audit" not in caplog.text
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_stream_success_audits_buffered_payload_when_capture_enabled(
        self, caplog
    ):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from app.routers.proxy import _handle_proxy

        caplog.set_level(logging.ERROR)

        chunk_one = b'data: {"usage":{"prompt_tokens":1,'
        chunk_two = b'"completion_tokens":1,"total_tokens":2}}\n\n'
        expected_payload = chunk_one + chunk_two

        class CompletedStreamResponse:
            def __init__(self):
                self.status_code = 200
                self.headers = {
                    "content-type": "text/event-stream",
                    "content-encoding": "gzip",
                    "content-length": "999",
                }
                self.closed = False

            async def aiter_bytes(self):
                yield chunk_one
                yield chunk_two

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
        upstream_resp = CompletedStreamResponse()
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
        model_config.vendor.audit_capture_bodies = True
        manager = BackgroundTaskManager()
        await manager.start()

        try:
            with (
                patch(
                    "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                    AsyncMock(return_value=model_config),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.build_attempt_plan",
                    return_value=_attempt_plan(endpoint),
                ),
                patch(
                    "app.routers.proxy._endpoint_is_active_now",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.load_costing_settings",
                    AsyncMock(return_value=MagicMock()),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.compute_cost_fields",
                    return_value={},
                ),
                patch(
                    "app.routers.proxy.log_request", AsyncMock(return_value=888)
                ) as log_mock,
                patch("app.routers.proxy.record_audit_log", AsyncMock()) as audit_mock,
                patch(
                    "app.routers.proxy_domains.attempt_outcome_reporting.background_task_manager",
                    manager,
                ),
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
                assert response.headers["content-type"] == "text/event-stream"
                assert "content-encoding" not in response.headers
                assert "content-length" not in response.headers

                stream = cast(AsyncGenerator[bytes, None], response.body_iterator)
                received = [chunk async for chunk in stream]

                await self._wait_for_asyncmock_calls(log_mock)
                await self._wait_for_asyncmock_calls(audit_mock)

                assert b"".join(received) == expected_payload
                assert upstream_resp.closed is True
                log_mock.assert_awaited_once()
                audit_mock.assert_awaited_once()
                audit_call = audit_mock.await_args
                assert audit_call is not None
                assert audit_call.kwargs["request_log_id"] == 888
                assert audit_call.kwargs["response_body"] == expected_payload
                assert audit_call.kwargs["capture_bodies"] is True
                assert audit_call.kwargs["response_headers"] == {
                    "content-type": "text/event-stream"
                }
                assert "Failed to log streaming request" not in caplog.text
                assert "Failed to log streaming audit" not in caplog.text
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_stream_success_without_body_capture_logs_tokens_without_audit_payload(
        self, caplog
    ):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from app.routers.proxy import _handle_proxy

        caplog.set_level(logging.ERROR)

        chunk_one = b'data: {"usage":{"prompt_tokens":1,'
        chunk_two = b'"completion_tokens":1,"total_tokens":2}}\n\n'
        expected_payload = chunk_one + chunk_two

        class CompletedStreamResponse:
            def __init__(self):
                self.status_code = 200
                self.headers = {"content-type": "text/event-stream"}
                self.closed = False

            async def aiter_bytes(self):
                yield chunk_one
                yield chunk_two

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
        upstream_resp = CompletedStreamResponse()
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
        model_config.vendor.audit_capture_bodies = False
        manager = BackgroundTaskManager()
        await manager.start()

        try:

            def build_cost_fields_for_assertion(**kwargs):
                return {
                    "cache_read_input_tokens": kwargs["cache_read_input_tokens"],
                    "cache_creation_input_tokens": kwargs[
                        "cache_creation_input_tokens"
                    ],
                    "reasoning_tokens": kwargs["reasoning_tokens"],
                }

            with (
                patch(
                    "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                    AsyncMock(return_value=model_config),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.build_attempt_plan",
                    return_value=_attempt_plan(endpoint),
                ),
                patch(
                    "app.routers.proxy._endpoint_is_active_now",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.load_costing_settings",
                    AsyncMock(return_value=MagicMock()),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.compute_cost_fields",
                    return_value={},
                ),
                patch(
                    "app.routers.proxy.log_request", AsyncMock(return_value=890)
                ) as log_mock,
                patch("app.routers.proxy.record_audit_log", AsyncMock()) as audit_mock,
                patch(
                    "app.routers.proxy_domains.attempt_outcome_reporting.background_task_manager",
                    manager,
                ),
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
                received = [chunk async for chunk in stream]

                await self._wait_for_asyncmock_calls(log_mock)
                await self._wait_for_asyncmock_calls(audit_mock)

                assert b"".join(received) == expected_payload
                assert upstream_resp.closed is True

                log_mock.assert_awaited_once()
                log_call = log_mock.await_args
                assert log_call is not None
                assert log_call.kwargs["input_tokens"] == 1
                assert log_call.kwargs["output_tokens"] == 1
                assert log_call.kwargs["total_tokens"] == 2

                audit_mock.assert_awaited_once()
                audit_call = audit_mock.await_args
                assert audit_call is not None
                assert audit_call.kwargs["request_log_id"] == 890
                assert audit_call.kwargs["capture_bodies"] is False
                assert audit_call.kwargs["response_body"] is None
                assert "Failed to log streaming request" not in caplog.text
                assert "Failed to log streaming audit" not in caplog.text
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_stream_success_audits_openai_response_completed_payload_when_capture_enabled(
        self, caplog
    ):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from app.routers.proxy import _handle_proxy

        caplog.set_level(logging.ERROR)

        delta_event = (
            b"event: response.output_text.delta\n"
            b'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
        )
        completed_event = (
            b"event: response.completed\n"
            b'data: {"type":"response.completed","response":{"id":"resp_123","usage":{"input_tokens":75,"output_tokens":125,"total_tokens":200,"input_tokens_details":{"cached_tokens":32},"output_tokens_details":{"reasoning_tokens":64}}}}\n\n'
        )
        expected_payload = delta_event + completed_event

        class CompletedStreamResponse:
            def __init__(self):
                self.status_code = 200
                self.headers = {"content-type": "text/event-stream"}
                self.closed = False

            async def aiter_bytes(self):
                yield delta_event
                yield completed_event

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
        upstream_resp = CompletedStreamResponse()
        app.state.http_client = DummyHttpClient(upstream_resp)

        raw_body = json.dumps(
            {
                "model": "gpt-4o-mini",
                "stream": True,
                "input": "hello",
            }
        ).encode("utf-8")
        request = self._build_request(app, raw_body)
        mock_db = self._build_db_mock()
        model_config, endpoint = self._build_model_config_and_endpoint()
        model_config.vendor.audit_capture_bodies = True
        manager = BackgroundTaskManager()
        await manager.start()

        try:

            def build_cost_fields_for_response_completed_usage(**kwargs):
                return {
                    "cache_read_input_tokens": kwargs["cache_read_input_tokens"],
                    "cache_creation_input_tokens": kwargs[
                        "cache_creation_input_tokens"
                    ],
                    "reasoning_tokens": kwargs["reasoning_tokens"],
                }

            with (
                patch(
                    "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                    AsyncMock(return_value=model_config),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.build_attempt_plan",
                    return_value=_attempt_plan(endpoint),
                ),
                patch(
                    "app.routers.proxy._endpoint_is_active_now",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.load_costing_settings",
                    AsyncMock(return_value=MagicMock()),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.compute_cost_fields",
                    side_effect=build_cost_fields_for_response_completed_usage,
                ),
                patch(
                    "app.routers.proxy.log_request", AsyncMock(return_value=893)
                ) as log_mock,
                patch("app.routers.proxy.record_audit_log", AsyncMock()) as audit_mock,
                patch(
                    "app.routers.proxy_domains.attempt_outcome_reporting.background_task_manager",
                    manager,
                ),
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
                received = [chunk async for chunk in stream]

                await self._wait_for_asyncmock_calls(log_mock)
                await self._wait_for_asyncmock_calls(audit_mock)

                assert b"".join(received) == expected_payload
                assert upstream_resp.closed is True

                log_mock.assert_awaited_once()
                log_call = log_mock.await_args
                assert log_call is not None
                assert log_call.kwargs["input_tokens"] == 75
                assert log_call.kwargs["output_tokens"] == 125
                assert log_call.kwargs["total_tokens"] == 200
                assert log_call.kwargs["cache_read_input_tokens"] == 32
                assert log_call.kwargs["cache_creation_input_tokens"] == 0
                assert log_call.kwargs["reasoning_tokens"] == 64

                audit_mock.assert_awaited_once()
                audit_call = audit_mock.await_args
                assert audit_call is not None
                assert audit_call.kwargs["request_log_id"] == 893
                assert audit_call.kwargs["capture_bodies"] is True
                assert audit_call.kwargs["response_body"] == expected_payload
                assert "Failed to log streaming request" not in caplog.text
                assert "Failed to log streaming audit" not in caplog.text
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_stream_success_logs_oversized_openai_response_completed_usage_without_body_capture(
        self, caplog
    ):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from app.routers.proxy import _handle_proxy

        caplog.set_level(logging.ERROR)

        large_prefix = "A" * 40_000
        large_suffix = "B" * 70_000
        completed_event = b"event: response.completed\n" + (
            'data: {"type":"response.completed","response":{"id":"resp_oversized","output":[{"type":"message","id":"msg_oversized","status":"completed","role":"assistant","content":[{"type":"output_text","text":"'
            + large_prefix
            + '"}]}],"usage":{"input_tokens":75,"output_tokens":125,"total_tokens":200,"input_tokens_details":{"cached_tokens":32},"output_tokens_details":{"reasoning_tokens":64}},"trace":"'
            + large_suffix
            + '"}}\n\n'
        ).encode("utf-8")
        expected_payload = completed_event

        class CompletedStreamResponse:
            def __init__(self):
                self.status_code = 200
                self.headers = {"content-type": "text/event-stream"}
                self.closed = False

            async def aiter_bytes(self):
                for offset in range(0, len(completed_event), 4096):
                    yield completed_event[offset : offset + 4096]

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
        upstream_resp = CompletedStreamResponse()
        app.state.http_client = DummyHttpClient(upstream_resp)

        raw_body = json.dumps(
            {
                "model": "gpt-5.4",
                "stream": True,
                "input": "hello",
            }
        ).encode("utf-8")
        request = self._build_request(app, raw_body)
        mock_db = self._build_db_mock()
        model_config, endpoint = self._build_model_config_and_endpoint(
            model_id="gpt-5.4"
        )
        model_config.vendor.audit_capture_bodies = False
        manager = BackgroundTaskManager()
        await manager.start()

        try:

            def build_cost_fields_for_oversized_usage(**kwargs):
                return {
                    "cache_read_input_tokens": kwargs["cache_read_input_tokens"],
                    "cache_creation_input_tokens": kwargs[
                        "cache_creation_input_tokens"
                    ],
                    "reasoning_tokens": kwargs["reasoning_tokens"],
                }

            with (
                patch(
                    "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                    AsyncMock(return_value=model_config),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.build_attempt_plan",
                    return_value=_attempt_plan(endpoint),
                ),
                patch(
                    "app.routers.proxy._endpoint_is_active_now",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.load_costing_settings",
                    AsyncMock(return_value=MagicMock()),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.compute_cost_fields",
                    side_effect=build_cost_fields_for_oversized_usage,
                ),
                patch(
                    "app.routers.proxy.log_request", AsyncMock(return_value=894)
                ) as log_mock,
                patch(
                    "app.routers.proxy.log_final_usage_request_event",
                    AsyncMock(return_value=995),
                ) as usage_event_mock,
                patch("app.routers.proxy.record_audit_log", AsyncMock()) as audit_mock,
                patch(
                    "app.routers.proxy_domains.attempt_outcome_reporting.background_task_manager",
                    manager,
                ),
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
                received = [chunk async for chunk in stream]

                await self._wait_for_asyncmock_calls(log_mock)
                await self._wait_for_asyncmock_calls(usage_event_mock)
                await self._wait_for_asyncmock_calls(audit_mock)

                assert b"".join(received) == expected_payload
                assert upstream_resp.closed is True

                log_mock.assert_awaited_once()
                log_call = log_mock.await_args
                assert log_call is not None
                assert log_call.kwargs["input_tokens"] == 75
                assert log_call.kwargs["output_tokens"] == 125
                assert log_call.kwargs["total_tokens"] == 200
                assert log_call.kwargs["cache_read_input_tokens"] == 32
                assert log_call.kwargs["cache_creation_input_tokens"] == 0
                assert log_call.kwargs["reasoning_tokens"] == 64

                usage_event_mock.assert_awaited_once()
                usage_event_call = usage_event_mock.await_args
                assert usage_event_call is not None
                assert usage_event_call.kwargs["input_tokens"] == 75
                assert usage_event_call.kwargs["output_tokens"] == 125
                assert usage_event_call.kwargs["total_tokens"] == 200

                audit_mock.assert_awaited_once()
                audit_call = audit_mock.await_args
                assert audit_call is not None
                assert audit_call.kwargs["request_log_id"] == 894
                assert audit_call.kwargs["capture_bodies"] is False
                assert audit_call.kwargs["response_body"] is None
                assert "Failed to log streaming request" not in caplog.text
                assert "Failed to log streaming audit" not in caplog.text
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_stream_success_without_body_capture_matches_full_payload_multi_event_usage(
        self, caplog
    ):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse

        from app.routers.proxy import _handle_proxy
        from app.services.stats_service import extract_token_usage

        caplog.set_level(logging.ERROR)

        event_one = (
            b'data: {"usage":{"prompt_tokens":10,"cache_read_input_tokens":7}}\n\n'
        )
        event_two = b'data: {"usage":{"completion_tokens":5}}\n\n'
        expected_payload = event_one + event_two
        expected_tokens = extract_token_usage(expected_payload)

        class CompletedStreamResponse:
            def __init__(self):
                self.status_code = 200
                self.headers = {"content-type": "text/event-stream"}
                self.closed = False

            async def aiter_bytes(self):
                yield event_one
                yield event_two

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
        upstream_resp = CompletedStreamResponse()
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
        model_config.vendor.audit_capture_bodies = False
        manager = BackgroundTaskManager()
        await manager.start()

        try:

            def build_cost_fields_for_multi_event_assertion(**kwargs):
                return {
                    "cache_read_input_tokens": kwargs["cache_read_input_tokens"],
                    "cache_creation_input_tokens": kwargs[
                        "cache_creation_input_tokens"
                    ],
                    "reasoning_tokens": kwargs["reasoning_tokens"],
                }

            with (
                patch(
                    "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                    AsyncMock(return_value=model_config),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.build_attempt_plan",
                    return_value=_attempt_plan(endpoint),
                ),
                patch(
                    "app.routers.proxy._endpoint_is_active_now",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.load_costing_settings",
                    AsyncMock(return_value=MagicMock()),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.compute_cost_fields",
                    side_effect=build_cost_fields_for_multi_event_assertion,
                ),
                patch(
                    "app.routers.proxy.log_request", AsyncMock(return_value=891)
                ) as log_mock,
                patch("app.routers.proxy.record_audit_log", AsyncMock()) as audit_mock,
                patch(
                    "app.routers.proxy_domains.attempt_outcome_reporting.background_task_manager",
                    manager,
                ),
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
                received = [chunk async for chunk in stream]

                await self._wait_for_asyncmock_calls(log_mock)
                await self._wait_for_asyncmock_calls(audit_mock)

                assert received == [event_one, event_two]
                assert b"".join(received) == expected_payload
                assert upstream_resp.closed is True

                log_mock.assert_awaited_once()
                log_call = log_mock.await_args
                assert log_call is not None
                assert (
                    log_call.kwargs["input_tokens"] == expected_tokens["input_tokens"]
                )
                assert (
                    log_call.kwargs["output_tokens"] == expected_tokens["output_tokens"]
                )
                assert (
                    log_call.kwargs["total_tokens"] == expected_tokens["total_tokens"]
                )
                assert (
                    log_call.kwargs["cache_read_input_tokens"]
                    == expected_tokens["cache_read_input_tokens"]
                )

                audit_mock.assert_awaited_once()
                audit_call = audit_mock.await_args
                assert audit_call is not None
                assert audit_call.kwargs["request_log_id"] == 891
                assert audit_call.kwargs["capture_bodies"] is False
                assert audit_call.kwargs["response_body"] is None
                assert "Failed to log streaming request" not in caplog.text
                assert "Failed to log streaming audit" not in caplog.text
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_gemini_stream_success_without_body_capture_logs_response_id(
        self, caplog
    ):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from app.routers.proxy import _handle_proxy

        caplog.set_level(logging.ERROR)

        chunk_one = b'data: {"responseId":"resp-gemini-1","served_by":"primary"}\n\n'
        chunk_two = (
            b'data: {"usageMetadata":{"promptTokenCount":1,'
            b'"candidatesTokenCount":1,"totalTokenCount":2}}\n\n'
        )
        expected_payload = chunk_one + chunk_two

        class CompletedStreamResponse:
            def __init__(self):
                self.status_code = 200
                self.headers = {"content-type": "text/event-stream"}
                self.closed = False

            async def aiter_bytes(self):
                yield chunk_one
                yield chunk_two

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
        upstream_resp = CompletedStreamResponse()
        app.state.http_client = DummyHttpClient(upstream_resp)

        request_path = "/v1beta/models/gemini-2.5-flash:streamGenerateContent"
        raw_body = json.dumps(
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": "hello"}],
                    }
                ]
            }
        ).encode("utf-8")
        request = self._build_request(
            app, raw_body, path=request_path, raw_path=request_path.encode("utf-8")
        )
        mock_db = self._build_db_mock()
        model_config, endpoint = self._build_model_config_and_endpoint(
            api_family="gemini",
            model_id="gemini-2.5-flash",
        )
        model_config.vendor.audit_capture_bodies = False
        manager = BackgroundTaskManager()
        await manager.start()

        try:
            with (
                patch(
                    "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                    AsyncMock(return_value=model_config),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.build_attempt_plan",
                    return_value=_attempt_plan(endpoint),
                ),
                patch(
                    "app.routers.proxy._endpoint_is_active_now",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.load_costing_settings",
                    AsyncMock(return_value=MagicMock()),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.compute_cost_fields",
                    return_value={},
                ),
                patch(
                    "app.routers.proxy.log_request", AsyncMock(return_value=892)
                ) as log_mock,
                patch("app.routers.proxy.record_audit_log", AsyncMock()) as audit_mock,
                patch(
                    "app.routers.proxy_domains.attempt_outcome_reporting.background_task_manager",
                    manager,
                ),
            ):
                response = await _handle_proxy(
                    request=request,
                    db=mock_db,
                    raw_body=raw_body,
                    request_path=request_path,
                    profile_id=1,
                )

                assert response.status_code == 200
                assert isinstance(response, StreamingResponse)

                stream = cast(AsyncGenerator[bytes, None], response.body_iterator)
                received = [chunk async for chunk in stream]

                await self._wait_for_asyncmock_calls(log_mock)
                await self._wait_for_asyncmock_calls(audit_mock)

                assert b"".join(received) == expected_payload
                assert upstream_resp.closed is True

                log_mock.assert_awaited_once()
                log_call = log_mock.await_args
                assert log_call is not None
                assert log_call.kwargs["provider_correlation_id"] == "resp-gemini-1"

                audit_mock.assert_awaited_once()
                assert "Failed to log streaming request" not in caplog.text
                assert "Failed to log streaming audit" not in caplog.text
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_stream_success_finalizes_request_log_and_audit_inline(self, caplog):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from app.routers.proxy import _handle_proxy

        caplog.set_level(logging.ERROR)

        chunk_one = b'data: {"usage":{"prompt_tokens":1,'
        chunk_two = b'"completion_tokens":1,"total_tokens":2}}\n\n'
        expected_payload = chunk_one + chunk_two

        class CompletedStreamResponse:
            def __init__(self):
                self.status_code = 200
                self.headers = {"content-type": "text/event-stream"}
                self.closed = False

            async def aiter_bytes(self):
                yield chunk_one
                yield chunk_two

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
        upstream_resp = CompletedStreamResponse()
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
        model_config.vendor.audit_capture_bodies = True

        with (
            patch(
                "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                AsyncMock(return_value=model_config),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.build_attempt_plan",
                return_value=_attempt_plan(endpoint),
            ),
            patch(
                "app.routers.proxy._endpoint_is_active_now",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.load_costing_settings",
                AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.compute_cost_fields",
                return_value={},
            ),
            patch(
                "app.routers.proxy.log_request", AsyncMock(return_value=889)
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
            received = [chunk async for chunk in stream]

            assert b"".join(received) == expected_payload
            assert upstream_resp.closed is True
            log_mock.assert_awaited_once()
            audit_mock.assert_awaited_once()
            assert "Failed to log streaming request" not in caplog.text
            assert "Failed to log streaming audit" not in caplog.text

    @pytest.mark.asyncio
    async def test_non_stream_response_and_audit_use_sanitized_headers(self):
        import httpx
        from fastapi import FastAPI
        from app.routers.proxy import _handle_proxy

        payload = json.dumps(
            {
                "id": "resp_123",
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        ).encode("utf-8")

        class DecodedResponse:
            def __init__(self):
                self.status_code = 200
                self.headers = {
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                    "content-length": "999",
                }
                self.content = payload

        class DummyHttpClient:
            def build_request(self, method: str, upstream_url: str, **kwargs):
                return httpx.Request(
                    method=method,
                    url=upstream_url,
                    headers=kwargs.get("headers"),
                    content=kwargs.get("content"),
                )

            async def send(self, request: httpx.Request, **kwargs):
                return DecodedResponse()

        app = FastAPI()
        app.state.http_client = DummyHttpClient()

        raw_body = json.dumps(
            {
                "model": "gpt-4o-mini",
                "input": "hello",
            }
        ).encode("utf-8")
        request = self._build_request(app, raw_body)
        mock_db = self._build_db_mock()
        model_config, endpoint = self._build_model_config_and_endpoint()
        model_config.vendor.audit_capture_bodies = True

        with (
            patch(
                "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                AsyncMock(return_value=model_config),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.build_attempt_plan",
                return_value=_attempt_plan(endpoint),
            ),
            patch(
                "app.routers.proxy._endpoint_is_active_now",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.load_costing_settings",
                AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.compute_cost_fields",
                return_value={},
            ),
            patch("app.routers.proxy.log_request", AsyncMock(return_value=444)),
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
            assert response.body == payload
            assert response.headers["content-type"] == "application/json"
            assert "content-encoding" not in response.headers
            assert response.headers["content-length"] == str(len(payload))

            audit_mock.assert_awaited_once()
            audit_call = audit_mock.await_args
            assert audit_call is not None
            assert audit_call.kwargs["response_headers"] == {
                "content-type": "application/json"
            }
            assert audit_call.kwargs["response_body"] == payload

    @pytest.mark.asyncio
    async def test_stream_error_response_and_audit_use_sanitized_headers(self):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from app.routers.proxy import _handle_proxy

        payload = json.dumps(
            {"error": {"message": "bad request", "type": "invalid_request"}}
        ).encode("utf-8")

        class ErrorStreamResponse:
            def __init__(self):
                self.status_code = 400
                self.headers = {
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                    "content-length": "999",
                }
                self.content = payload
                self.closed = False

            async def aread(self):
                return payload

            async def aclose(self):
                self.closed = True

        class DummyHttpClient:
            def __init__(self):
                self.response = ErrorStreamResponse()

            def build_request(self, method: str, upstream_url: str, **kwargs):
                return httpx.Request(
                    method=method,
                    url=upstream_url,
                    headers=kwargs.get("headers"),
                    content=kwargs.get("content"),
                )

            async def send(self, request: httpx.Request, **kwargs):
                assert kwargs.get("stream") is True
                return self.response

        app = FastAPI()
        app.state.http_client = DummyHttpClient()

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
        model_config.vendor.audit_capture_bodies = True

        with (
            patch(
                "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                AsyncMock(return_value=model_config),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.build_attempt_plan",
                return_value=_attempt_plan(endpoint),
            ),
            patch(
                "app.routers.proxy._endpoint_is_active_now",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.load_costing_settings",
                AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.compute_cost_fields",
                return_value={},
            ),
            patch("app.routers.proxy.log_request", AsyncMock(return_value=445)),
            patch("app.routers.proxy.record_audit_log", AsyncMock()) as audit_mock,
        ):
            response = await _handle_proxy(
                request=request,
                db=mock_db,
                raw_body=raw_body,
                request_path="/v1/responses",
                profile_id=1,
            )

            assert response.status_code == 400
            assert not isinstance(response, StreamingResponse)
            assert response.body == payload
            assert response.headers["content-type"] == "application/json"
            assert "content-encoding" not in response.headers
            assert response.headers["content-length"] == str(len(payload))
            assert app.state.http_client.response.closed is True

            audit_mock.assert_awaited_once()
            audit_call = audit_mock.await_args
            assert audit_call is not None
            assert audit_call.kwargs["response_headers"] == {
                "content-type": "application/json"
            }
            assert audit_call.kwargs["response_body"] == payload

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
        log_started = asyncio.Event()
        release_log = asyncio.Event()
        manager = BackgroundTaskManager()
        await manager.start()

        async def delayed_log_request(*args, **kwargs):
            log_started.set()
            await release_log.wait()
            return 777

        try:
            with (
                patch(
                    "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                    AsyncMock(return_value=model_config),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.build_attempt_plan",
                    return_value=_attempt_plan(endpoint),
                ),
                patch(
                    "app.routers.proxy._endpoint_is_active_now",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.load_costing_settings",
                    AsyncMock(return_value=MagicMock()),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.compute_cost_fields",
                    return_value={},
                ),
                patch(
                    "app.routers.proxy.log_request",
                    AsyncMock(side_effect=delayed_log_request),
                ) as log_mock,
                patch("app.routers.proxy.record_audit_log", AsyncMock()) as audit_mock,
                patch(
                    "app.routers.proxy_domains.attempt_outcome_reporting.background_task_manager",
                    manager,
                ),
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
                close_task = asyncio.create_task(stream.aclose())
                await asyncio.wait_for(log_started.wait(), timeout=1)
                assert audit_mock.await_count == 0
                release_log.set()
                await close_task

                await self._wait_for_asyncmock_calls(log_mock)
                await self._wait_for_asyncmock_calls(audit_mock)

                assert upstream_resp.closed is True
                log_mock.assert_awaited_once()
                audit_mock.assert_awaited_once()
                audit_call = audit_mock.await_args
                assert audit_call is not None
                assert audit_call.kwargs["request_log_id"] == 777
                assert "Failed to log streaming request" not in caplog.text
                assert "Failed to log streaming audit" not in caplog.text
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_stream_generator_close_releases_limiter_lease(self, caplog):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse

        from app.routers.proxy import _handle_proxy
        from app.services.loadbalancer.limiter import LimiterAcquireResult

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
        endpoint.max_in_flight_stream = 1
        log_started = asyncio.Event()
        release_log = asyncio.Event()
        manager = BackgroundTaskManager()
        await manager.start()
        release_connection_lease = AsyncMock()

        async def delayed_log_request(*args, **kwargs):
            log_started.set()
            await release_log.wait()
            return 778

        try:
            with (
                patch(
                    "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                    AsyncMock(return_value=model_config),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.build_attempt_plan",
                    return_value=_attempt_plan(endpoint),
                ),
                patch(
                    "app.routers.proxy._endpoint_is_active_now",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.load_costing_settings",
                    AsyncMock(return_value=MagicMock()),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.compute_cost_fields",
                    return_value={},
                ),
                patch(
                    "app.routers.proxy.log_request",
                    AsyncMock(side_effect=delayed_log_request),
                ) as log_mock,
                patch("app.routers.proxy.record_audit_log", AsyncMock()) as audit_mock,
                patch(
                    "app.routers.proxy.acquire_connection_limit",
                    AsyncMock(
                        return_value=LimiterAcquireResult(
                            admitted=True,
                            lease_token="stream-lease",
                        )
                    ),
                ),
                patch(
                    "app.routers.proxy.release_connection_lease",
                    release_connection_lease,
                ),
                patch(
                    "app.routers.proxy_domains.attempt_outcome_reporting.background_task_manager",
                    manager,
                ),
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
                close_task = asyncio.create_task(stream.aclose())
                await asyncio.wait_for(log_started.wait(), timeout=1)
                release_log.set()
                await close_task

                await self._wait_for_asyncmock_calls(log_mock)
                await self._wait_for_asyncmock_calls(audit_mock)

                release_connection_lease.assert_awaited_once()
                release_call = release_connection_lease.await_args
                assert release_call is not None
                assert release_call.kwargs["lease_token"] == "stream-lease"
                assert upstream_resp.closed is True
                assert "Failed to log streaming request" not in caplog.text
        finally:
            release_log.set()
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_long_lived_stream_heartbeats_limiter_lease_before_release(self):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse

        from app.routers.proxy import _handle_proxy
        from app.services.loadbalancer.limiter import LimiterAcquireResult

        class SlowHeartbeatStreamResponse:
            def __init__(self):
                self.status_code = 200
                self.headers = {"content-type": "text/event-stream"}
                self.closed = False
                self.continue_stream = asyncio.Event()

            async def aiter_bytes(self):
                yield (
                    b'data: {"usage":{"prompt_tokens":1,"completion_tokens":1,'
                    b'"total_tokens":2}}\n\n'
                )
                await self.continue_stream.wait()
                yield b"data: [DONE]\n\n"

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
        upstream_resp = SlowHeartbeatStreamResponse()
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
        model_config, connection = self._build_model_config_and_endpoint()
        connection.max_in_flight_stream = 1
        manager = BackgroundTaskManager()
        await manager.start()
        heartbeat_connection_lease = AsyncMock(return_value=True)
        release_connection_lease = AsyncMock()

        try:
            with (
                patch(
                    "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                    AsyncMock(return_value=model_config),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.build_attempt_plan",
                    return_value=_attempt_plan(connection),
                ),
                patch(
                    "app.routers.proxy._endpoint_is_active_now",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.load_costing_settings",
                    AsyncMock(return_value=MagicMock()),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.compute_cost_fields",
                    return_value={},
                ),
                patch("app.routers.proxy.log_request", AsyncMock(return_value=778)),
                patch("app.routers.proxy.record_audit_log", AsyncMock()),
                patch(
                    "app.routers.proxy.acquire_connection_limit",
                    AsyncMock(
                        return_value=LimiterAcquireResult(
                            admitted=True,
                            lease_token="stream-lease",
                        )
                    ),
                ),
                patch(
                    "app.routers.proxy.release_connection_lease",
                    release_connection_lease,
                ),
                patch(
                    "app.routers.proxy.heartbeat_connection_lease",
                    heartbeat_connection_lease,
                    create=True,
                ),
                patch(
                    "app.routers.proxy_domains.attempt_execution.DEFAULT_LIMITER_LEASE_TTL_SECONDS",
                    1,
                ),
                patch(
                    "app.routers.proxy_domains.attempt_outcome_reporting.background_task_manager",
                    manager,
                ),
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

                for _ in range(20):
                    if heartbeat_connection_lease.await_count > 0:
                        break
                    await asyncio.sleep(0.05)

                upstream_resp.continue_stream.set()
                async for _ in stream:
                    pass

                assert heartbeat_connection_lease.await_count >= 1
                heartbeat_call = heartbeat_connection_lease.await_args
                assert heartbeat_call is not None
                assert heartbeat_call.kwargs["lease_token"] == "stream-lease"
                release_connection_lease.assert_awaited_once()
                assert upstream_resp.closed is True
        finally:
            upstream_resp.continue_stream.set()
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_mid_stream_failure_is_accounted_as_failure_not_recovery(self):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse

        from app.routers.proxy import _handle_proxy
        from app.services.loadbalancer.limiter import LimiterAcquireResult

        class BrokenStreamResponse:
            def __init__(self):
                self.status_code = 200
                self.headers = {"content-type": "text/event-stream"}
                self.closed = False

            async def aiter_bytes(self):
                yield (
                    b'data: {"usage":{"prompt_tokens":1,"completion_tokens":1,'
                    b'"total_tokens":2}}\n\n'
                )
                raise RuntimeError("upstream stream broke")

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
        upstream_resp = BrokenStreamResponse()
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
        model_config, connection = self._build_model_config_and_endpoint()
        connection.max_in_flight_stream = 1
        manager = BackgroundTaskManager()
        await manager.start()
        log_request = AsyncMock(return_value=999)
        record_connection_failure = AsyncMock()
        record_connection_recovery = AsyncMock()

        try:
            with (
                patch(
                    "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                    AsyncMock(return_value=model_config),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.build_attempt_plan",
                    return_value=_attempt_plan(connection),
                ),
                patch(
                    "app.routers.proxy._endpoint_is_active_now",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.load_costing_settings",
                    AsyncMock(return_value=MagicMock()),
                ),
                patch(
                    "app.routers.proxy_domains.request_setup.compute_cost_fields",
                    return_value={},
                ),
                patch("app.routers.proxy.log_request", log_request),
                patch("app.routers.proxy.record_audit_log", AsyncMock()),
                patch(
                    "app.routers.proxy.acquire_connection_limit",
                    AsyncMock(
                        return_value=LimiterAcquireResult(
                            admitted=True,
                            lease_token="stream-lease",
                        )
                    ),
                ),
                patch(
                    "app.routers.proxy.release_connection_lease",
                    AsyncMock(),
                ),
                patch(
                    "app.routers.proxy.record_connection_failure",
                    record_connection_failure,
                ),
                patch(
                    "app.routers.proxy.record_connection_recovery",
                    record_connection_recovery,
                ),
                patch(
                    "app.routers.proxy.heartbeat_connection_lease",
                    AsyncMock(return_value=True),
                    create=True,
                ),
                patch(
                    "app.routers.proxy_domains.attempt_outcome_reporting.background_task_manager",
                    manager,
                ),
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
                async for _ in stream:
                    pass

                await self._wait_for_asyncmock_calls(log_request)

                record_connection_failure.assert_awaited_once()
                record_connection_recovery.assert_not_awaited()
                log_call = log_request.await_args
                assert log_call is not None
                assert log_call.kwargs["status_code"] == 0
                assert log_call.kwargs["is_stream"] is True
                assert upstream_resp.closed is True
        finally:
            await manager.shutdown()
