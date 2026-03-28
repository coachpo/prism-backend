import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from app.services.loadbalancer.types import AttemptPlan


def _attempt_plan(*connections):
    return AttemptPlan(
        connections=list(connections),
        blocked_connection_ids=[],
        probe_eligible_connection_ids=[],
    )


class TestDEF083_ProxyTargetRuntimeSelection:
    @pytest.mark.asyncio
    async def test_get_model_config_with_connections_rejects_proxy_with_zero_targets(
        self,
    ):
        from app.services.loadbalancer.planner import (
            ProxyTargetsUnroutableError,
            get_model_config_with_connections,
        )

        proxy_model = SimpleNamespace(
            profile_id=5,
            model_id="alias-model",
            model_type="proxy",
            proxy_targets=[],
        )

        first_result = MagicMock()
        first_result.scalar_one_or_none.return_value = proxy_model

        db = AsyncMock()
        db.execute = AsyncMock(return_value=first_result)

        with pytest.raises(ProxyTargetsUnroutableError) as exc_info:
            await get_model_config_with_connections(
                db=db,
                profile_id=5,
                model_id="alias-model",
            )

        assert exc_info.value.proxy_model_id == "alias-model"

    @pytest.mark.asyncio
    async def test_prepare_proxy_request_rejects_proxy_when_no_target_yields_attempt_plan(
        self,
    ):
        from app.routers.proxy_domains.request_setup import (
            ProxyRoutingRejection,
            prepare_proxy_request,
        )
        from app.services.loadbalancer.planner import ProxyTargetsUnroutableError

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
            audit_enabled=False,
            audit_capture_bodies=False,
            id=4,
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
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_requested_model_result)

        with patch(
            "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
            AsyncMock(
                side_effect=ProxyTargetsUnroutableError(proxy_model_id="proxy-model")
            ),
        ):
            with pytest.raises(ProxyRoutingRejection) as exc_info:
                await prepare_proxy_request(
                    request=request,
                    db=mock_db,
                    raw_body=raw_body,
                    request_path="/v1/chat/completions",
                    profile_id=1,
                )

        assert (
            exc_info.value.detail
            == "Proxy model 'proxy-model' has no routable targets."
        )
        assert exc_info.value.model_id == "proxy-model"
        assert exc_info.value.api_family == "openai"
        assert exc_info.value.vendor_id == 4
        assert exc_info.value.vendor_key == "openrouter"
        assert exc_info.value.vendor_name == "OpenRouter"

    @pytest.mark.asyncio
    async def test_proxy_request_logs_requested_and_resolved_target_without_cross_target_retry(
        self,
    ):
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
                    status_code=500,
                    request=request,
                    headers={"content-type": "application/json"},
                    content=b'{"error":{"message":"selected target failed"}}',
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
            {"model": "proxy-model", "messages": [{"role": "user", "content": "hi"}]}
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
            failover_ban_mode="off",
            failover_max_cooldown_strikes_before_ban=0,
            failover_ban_duration_seconds=0,
        )
        target_endpoint = MagicMock()
        target_endpoint.base_url = "https://target-a.example.com/v1"
        selected_connection = MagicMock()
        selected_connection.id = 1001
        selected_connection.endpoint_id = 501
        selected_connection.endpoint_rel = target_endpoint
        selected_connection.pricing_template_rel = None
        selected_connection.name = "target-a-conn"
        selected_connection.custom_headers = None
        selected_connection.auth_type = None

        resolved_target_model = SimpleNamespace(
            provider=vendor,
            vendor=vendor,
            api_family="openai",
            model_id="target-model-a",
            loadbalance_strategy=strategy,
            connections=[selected_connection],
        )

        mock_rules_result = MagicMock()
        mock_rules_result.scalars.return_value.all.return_value = []
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_rules_result)
        log_request = AsyncMock(return_value=901)

        with (
            patch(
                "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                AsyncMock(return_value=resolved_target_model),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.build_attempt_plan",
                AsyncMock(return_value=_attempt_plan(selected_connection)),
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
            with pytest.raises(HTTPException) as exc_info:
                await _handle_proxy(
                    request=request,
                    db=mock_db,
                    raw_body=raw_body,
                    request_path="/v1/chat/completions",
                    profile_id=1,
                )

        assert exc_info.value.status_code == 502
        assert app.state.http_client.sent_urls == [
            "https://target-a.example.com/v1/v1/chat/completions"
        ]
        log_request.assert_awaited_once()
        log_call = log_request.await_args
        assert log_call is not None
        log_kwargs = log_call.kwargs
        assert log_kwargs["model_id"] == "proxy-model"
        assert log_kwargs["resolved_target_model_id"] == "target-model-a"
