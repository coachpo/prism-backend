import asyncio
import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from app.models.models import Connection
from app.services.loadbalancer.policy import resolve_effective_loadbalance_policy
from app.services.loadbalancer.types import (
    AttemptCandidate,
    AttemptCandidateScoreInput,
    AttemptPlan,
)
from tests.loadbalance_strategy_helpers import make_routing_policy_adaptive


def _attempt_candidate(connection: Connection) -> AttemptCandidate:
    return AttemptCandidate(
        connection=connection,
        score_input=AttemptCandidateScoreInput(
            connection=connection,
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
        sort_key=(0.0, getattr(connection, "priority", 0), connection.id),
    )


def _attempt_plan(policy, *connections: Connection) -> AttemptPlan:
    resolved_policy = resolve_effective_loadbalance_policy(
        SimpleNamespace(routing_policy=policy)
    )
    return AttemptPlan(
        policy=resolved_policy,
        candidates=[_attempt_candidate(connection) for connection in connections],
        blocked_connection_ids=[],
        probe_eligible_connection_ids=[],
    )


def _build_request(app: FastAPI, raw_body: bytes) -> Request:
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


def _connection(connection_id: int, base_url: str, *, priority: int) -> Connection:
    endpoint = SimpleNamespace(
        id=connection_id + 500,
        endpoint_id=connection_id + 500,
        name=f"endpoint-{connection_id}",
        base_url=base_url,
        api_key="sk-test",
    )
    return cast(
        Connection,
        cast(
            object,
            SimpleNamespace(
                id=connection_id,
                endpoint_id=endpoint.id,
                endpoint_rel=endpoint,
                pricing_template_rel=None,
                name=f"connection-{connection_id}",
                custom_headers=None,
                auth_type=None,
                qps_limit=None,
                max_in_flight_non_stream=None,
                max_in_flight_stream=None,
                priority=priority,
                health_status="healthy",
                is_active=True,
            ),
        ),
    )


class TestDEF088_AdaptiveRoutingDeadlineAndHedging:
    @pytest.mark.asyncio
    async def test_request_deadline_exhaustion_stops_before_retrying_remaining_candidates(
        self,
    ):
        from app.routers.proxy import _handle_proxy

        app = FastAPI()
        app.state.http_client = object()
        raw_body = json.dumps(
            {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
        ).encode("utf-8")
        request = _build_request(app, raw_body)
        primary = _connection(1001, "https://primary.example.com/v1", priority=0)
        secondary = _connection(1002, "https://secondary.example.com/v1", priority=1)
        vendor = SimpleNamespace(
            key="openai",
            name="OpenAI",
            audit_enabled=False,
            audit_capture_bodies=False,
            id=1,
        )
        routing_policy = make_routing_policy_adaptive(
            deadline_budget_ms=15,
            hedge_enabled=False,
        )
        model_config = SimpleNamespace(
            vendor=vendor,
            api_family="openai",
            model_id="gpt-4o-mini",
            loadbalance_strategy=SimpleNamespace(routing_policy=routing_policy),
            connections=[primary, secondary],
        )
        mock_rules_result = MagicMock()
        mock_rules_result.scalars.return_value.all.return_value = []
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_rules_result)
        requested_urls: list[str] = []

        async def proxy_request(_client, method, upstream_url, headers, body):
            _ = (method, headers, body)
            requested_urls.append(upstream_url)
            if "primary" in upstream_url:
                await asyncio.sleep(0.05)
                return httpx.Response(
                    status_code=500,
                    request=httpx.Request("POST", upstream_url),
                    headers={"content-type": "application/json"},
                    content=b'{"error":{"message":"late primary failure"}}',
                )
            return httpx.Response(
                status_code=200,
                request=httpx.Request("POST", upstream_url),
                headers={"content-type": "application/json"},
                content=b'{"id":"secondary-success"}',
            )

        with (
            patch(
                "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                AsyncMock(return_value=model_config),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.build_attempt_plan",
                AsyncMock(
                    return_value=_attempt_plan(routing_policy, primary, secondary)
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
                "app.routers.proxy.proxy_request", AsyncMock(side_effect=proxy_request)
            ),
            patch("app.routers.proxy.log_request", AsyncMock(return_value=901)),
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

        assert exc_info.value.status_code == 504
        assert requested_urls == ["https://primary.example.com/v1/v1/chat/completions"]

    @pytest.mark.asyncio
    async def test_slow_primary_launches_one_hedge_and_commits_first_successful_response(
        self,
    ):
        from app.routers.proxy import _handle_proxy

        app = FastAPI()
        app.state.http_client = object()
        raw_body = json.dumps(
            {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
        ).encode("utf-8")
        request = _build_request(app, raw_body)
        primary = _connection(2001, "https://primary.example.com/v1", priority=0)
        hedge = _connection(2002, "https://hedge.example.com/v1", priority=1)
        tertiary = _connection(2003, "https://tertiary.example.com/v1", priority=2)
        vendor = SimpleNamespace(
            key="openai",
            name="OpenAI",
            audit_enabled=False,
            audit_capture_bodies=False,
            id=1,
        )
        routing_policy = make_routing_policy_adaptive(
            deadline_budget_ms=500,
            hedge_enabled=True,
            hedge_delay_ms=10,
            max_additional_attempts=1,
        )
        model_config = SimpleNamespace(
            vendor=vendor,
            api_family="openai",
            model_id="gpt-4o-mini",
            loadbalance_strategy=SimpleNamespace(routing_policy=routing_policy),
            connections=[primary, hedge, tertiary],
        )
        mock_rules_result = MagicMock()
        mock_rules_result.scalars.return_value.all.return_value = []
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_rules_result)
        requested_urls: list[str] = []

        async def proxy_request(_client, method, upstream_url, headers, body):
            _ = (method, headers, body)
            requested_urls.append(upstream_url)
            if "primary" in upstream_url:
                await asyncio.sleep(0.05)
                return httpx.Response(
                    status_code=200,
                    request=httpx.Request("POST", upstream_url),
                    headers={"content-type": "application/json"},
                    content=b'{"id":"primary-success"}',
                )
            if "hedge" in upstream_url:
                await asyncio.sleep(0.01)
                return httpx.Response(
                    status_code=200,
                    request=httpx.Request("POST", upstream_url),
                    headers={"content-type": "application/json"},
                    content=b'{"id":"hedge-success"}',
                )
            return httpx.Response(
                status_code=200,
                request=httpx.Request("POST", upstream_url),
                headers={"content-type": "application/json"},
                content=b'{"id":"tertiary-success"}',
            )

        with (
            patch(
                "app.routers.proxy_domains.request_setup.get_model_config_with_connections",
                AsyncMock(return_value=model_config),
            ),
            patch(
                "app.routers.proxy_domains.request_setup.build_attempt_plan",
                AsyncMock(
                    return_value=_attempt_plan(routing_policy, primary, hedge, tertiary)
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
                "app.routers.proxy.proxy_request", AsyncMock(side_effect=proxy_request)
            ),
            patch("app.routers.proxy.log_request", AsyncMock(return_value=902)),
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
        assert json.loads(bytes(response.body)) == {"id": "hedge-success"}
        assert requested_urls == [
            "https://primary.example.com/v1/v1/chat/completions",
            "https://hedge.example.com/v1/v1/chat/completions",
        ]
