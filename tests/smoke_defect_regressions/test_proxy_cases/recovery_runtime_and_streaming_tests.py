import asyncio
import json
import logging
from typing import AsyncGenerator, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


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
    async def test_proxy_skips_endpoint_disabled_after_plan_and_uses_next_endpoint(
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
            ModelConfig,
            Profile,
            Provider,
        )
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
                    plan = real_build_attempt_plan(profile_id, model_config, now_mono)
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
        model_config.provider.audit_capture_bodies = True

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
            audit_call = audit_mock.await_args
            assert audit_call is not None
            assert audit_call.kwargs["response_body"] == first
            assert audit_call.kwargs["capture_bodies"] is True
            assert "Failed to log streaming request" not in caplog.text
            assert "Failed to record streaming audit log" not in caplog.text

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
        model_config.provider.audit_capture_bodies = True

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
                "app.routers.proxy.log_request", AsyncMock(return_value=888)
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
            assert audit_call.kwargs["response_body"] == expected_payload
            assert audit_call.kwargs["capture_bodies"] is True
            assert audit_call.kwargs["response_headers"] == {
                "content-type": "text/event-stream"
            }
            assert "Failed to log streaming request" not in caplog.text
            assert "Failed to record streaming audit log" not in caplog.text

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
        model_config.provider.audit_capture_bodies = True

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
        model_config.provider.audit_capture_bodies = True

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
