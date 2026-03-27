import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from starlette.requests import Request

from app.core.database import AsyncSessionLocal
from app.models.models import (
    Connection,
    Endpoint,
    ModelConfig,
    Profile,
    ProxyApiKey,
    RequestLog,
    UsageRequestEvent,
    Vendor,
)
from app.routers.proxy import _handle_proxy
from tests.loadbalance_strategy_helpers import make_loadbalance_strategy


@dataclass(slots=True)
class RuntimeRouteSeed:
    connection_ids: list[int]
    endpoint_ids: list[int]
    model_id: str
    profile_id: int
    proxy_api_key_id: int
    proxy_api_key_name: str


class BufferedHttpClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses

    def build_request(self, method: str, upstream_url: str, **kwargs) -> httpx.Request:
        return httpx.Request(
            method=method,
            url=upstream_url,
            headers=kwargs.get("headers"),
            content=kwargs.get("content"),
        )

    async def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        response = self._responses.pop(0)
        response.request = request
        return response


class StreamingHttpClient:
    def __init__(self, upstream_response: object) -> None:
        self._upstream_response = upstream_response

    def build_request(self, method: str, upstream_url: str, **kwargs) -> httpx.Request:
        return httpx.Request(
            method=method,
            url=upstream_url,
            headers=kwargs.get("headers"),
            content=kwargs.get("content"),
        )

    async def send(self, request: httpx.Request, **kwargs) -> object:
        assert kwargs.get("stream") is True
        return self._upstream_response


class CompletedStreamResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self.closed = False
        self.headers = {"content-type": "text/event-stream"}
        self.status_code = 200
        self._chunks = chunks

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _build_request(
    *,
    app: FastAPI,
    path: str,
    proxy_api_key_id: int | None = None,
    proxy_api_key_name: str | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": headers
            or [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "scheme": "http",
            "app": app,
        }
    )
    request.state.proxy_api_key_id = proxy_api_key_id
    request.state.proxy_api_key_name = proxy_api_key_name
    return request


def _json_response(status_code: int, payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        headers={"content-type": "application/json"},
        content=json.dumps(payload).encode("utf-8"),
    )


async def _consume_streaming_response(response: StreamingResponse) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            chunks.append(chunk)
            continue
        if isinstance(chunk, memoryview):
            chunks.append(chunk.tobytes())
            continue
        chunks.append(str(chunk).encode("utf-8"))
    await asyncio.sleep(0)
    return b"".join(chunks)


async def _load_request_logs(profile_id: int) -> list[RequestLog]:
    async with AsyncSessionLocal() as db:
        return list(
            (
                await db.execute(
                    select(RequestLog)
                    .where(RequestLog.profile_id == profile_id)
                    .order_by(RequestLog.attempt_number.asc(), RequestLog.id.asc())
                )
            )
            .scalars()
            .all()
        )


async def _load_usage_events(profile_id: int) -> list[UsageRequestEvent]:
    async with AsyncSessionLocal() as db:
        return list(
            (
                await db.execute(
                    select(UsageRequestEvent)
                    .where(UsageRequestEvent.profile_id == profile_id)
                    .order_by(UsageRequestEvent.id.asc())
                )
            )
            .scalars()
            .all()
        )


async def _seed_runtime_route(*, connection_count: int) -> RuntimeRouteSeed:
    suffix = uuid4().hex[:8]
    model_id = f"gpt-4o-mini-{suffix}"

    async with AsyncSessionLocal() as db:
        profile = Profile(
            name=f"usage-event-profile-{suffix}",
            is_active=False,
            is_default=False,
            version=0,
        )
        vendor = Vendor(
            key=f"usage-event-vendor-{suffix}",
            name=f"Usage Event Vendor {suffix}",
            audit_enabled=False,
            audit_capture_bodies=False,
        )
        strategy = make_loadbalance_strategy(profile=profile, strategy_type="failover")
        model = ModelConfig(
            profile=profile,
            vendor=vendor,
            api_family="openai",
            model_id=model_id,
            display_name=f"Usage Event Model {suffix}",
            model_type="native",
            loadbalance_strategy=strategy,
            is_enabled=True,
        )
        proxy_api_key = ProxyApiKey(
            name=f"Runtime Key {suffix}",
            key_prefix=f"prism_pk_{suffix}",
            key_hash=(uuid4().hex * 2)[:64],
            last_four=suffix[-4:],
            is_active=True,
        )

        db.add_all([profile, vendor, strategy, model, proxy_api_key])

        endpoints: list[Endpoint] = []
        connections: list[Connection] = []
        for index in range(connection_count):
            endpoint = Endpoint(
                name=f"endpoint-{index}-{suffix}",
                profile=profile,
                base_url=f"https://endpoint-{index}-{suffix}.example.com/v1",
                api_key=f"sk-{index}-{suffix}",
                position=index,
            )
            connection = Connection(
                profile=profile,
                model_config_rel=model,
                endpoint_rel=endpoint,
                is_active=True,
                priority=index,
                name=f"connection-{index + 1}",
            )
            db.add_all([endpoint, connection])
            endpoints.append(endpoint)
            connections.append(connection)

        await db.commit()

        return RuntimeRouteSeed(
            connection_ids=[connection.id for connection in connections],
            endpoint_ids=[endpoint.id for endpoint in endpoints],
            model_id=model_id,
            profile_id=profile.id,
            proxy_api_key_id=proxy_api_key.id,
            proxy_api_key_name=proxy_api_key.name,
        )


@pytest.mark.asyncio
async def test_proxy_auth_middleware_stores_proxy_key_name_on_request_state() -> None:
    from app.bootstrap.auth_middleware import _handle_proxy_authentication

    app = FastAPI()
    request = _build_request(
        app=app,
        path="/v1/chat/completions",
        headers=[(b"host", b"testserver"), (b"x-api-key", b"prism-key")],
    )
    call_next = AsyncMock(return_value=Response(status_code=204))
    proxy_key = SimpleNamespace(id=41, name="Primary Runtime Key")

    with (
        patch(
            "app.bootstrap.auth_middleware.verify_proxy_api_key",
            AsyncMock(return_value=proxy_key),
        ),
        patch(
            "app.bootstrap.auth_middleware.enqueue_proxy_api_key_usage",
            return_value=True,
        ),
    ):
        response = await _handle_proxy_authentication(
            request,
            call_next,
            auth_settings=SimpleNamespace(auth_enabled=True),
            session=AsyncMock(),
        )

    assert response.status_code == 204
    assert request.state.proxy_api_key_id == 41
    assert request.state.proxy_api_key_name == "Primary Runtime Key"


@pytest.mark.asyncio
async def test_buffered_failover_keeps_attempt_request_logs_but_writes_one_final_usage_event() -> (
    None
):
    seed = await _seed_runtime_route(connection_count=2)
    raw_body = json.dumps(
        {"model": seed.model_id, "messages": [{"role": "user", "content": "hi"}]}
    ).encode("utf-8")
    app = FastAPI()
    app.state.http_client = BufferedHttpClient(
        [
            _json_response(500, {"error": {"message": "retry"}}),
            _json_response(
                200,
                {
                    "id": "chatcmpl-ok",
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 5,
                        "total_tokens": 8,
                    },
                },
            ),
        ]
    )
    request = _build_request(
        app=app,
        path="/v1/chat/completions",
        proxy_api_key_id=seed.proxy_api_key_id,
        proxy_api_key_name=seed.proxy_api_key_name,
    )

    async with AsyncSessionLocal() as db:
        response = await _handle_proxy(
            request=request,
            db=db,
            raw_body=raw_body,
            request_path="/v1/chat/completions",
            profile_id=seed.profile_id,
        )

    request_logs = await _load_request_logs(seed.profile_id)
    usage_events = await _load_usage_events(seed.profile_id)

    assert response.status_code == 200
    assert [entry.attempt_number for entry in request_logs] == [1, 2]
    assert [entry.proxy_api_key_id for entry in request_logs] == [
        seed.proxy_api_key_id,
        seed.proxy_api_key_id,
    ]
    assert [entry.proxy_api_key_name_snapshot for entry in request_logs] == [
        seed.proxy_api_key_name,
        seed.proxy_api_key_name,
    ]
    assert len(usage_events) == 1
    assert usage_events[0].attempt_count == 2
    assert usage_events[0].status_code == 200
    assert usage_events[0].connection_id == seed.connection_ids[1]
    assert usage_events[0].endpoint_id == seed.endpoint_ids[1]
    assert usage_events[0].proxy_api_key_id == seed.proxy_api_key_id
    assert usage_events[0].proxy_api_key_name_snapshot == seed.proxy_api_key_name
    assert usage_events[0].total_tokens == 8
    assert usage_events[0].ingress_request_id == request_logs[0].ingress_request_id


@pytest.mark.asyncio
async def test_streaming_terminal_error_writes_one_final_usage_event() -> None:
    seed = await _seed_runtime_route(connection_count=1)
    raw_body = json.dumps(
        {
            "model": seed.model_id,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode("utf-8")
    app = FastAPI()
    app.state.http_client = StreamingHttpClient(
        _json_response(404, {"error": {"message": "not found"}})
    )
    request = _build_request(
        app=app,
        path="/v1/responses",
        proxy_api_key_id=seed.proxy_api_key_id,
        proxy_api_key_name=seed.proxy_api_key_name,
    )

    async with AsyncSessionLocal() as db:
        response = await _handle_proxy(
            request=request,
            db=db,
            raw_body=raw_body,
            request_path="/v1/responses",
            profile_id=seed.profile_id,
        )

    request_logs = await _load_request_logs(seed.profile_id)
    usage_events = await _load_usage_events(seed.profile_id)

    assert response.status_code == 404
    assert isinstance(response, Response)
    assert len(request_logs) == 1
    assert request_logs[0].proxy_api_key_id == seed.proxy_api_key_id
    assert request_logs[0].proxy_api_key_name_snapshot == seed.proxy_api_key_name
    assert len(usage_events) == 1
    assert usage_events[0].status_code == 404
    assert usage_events[0].attempt_count == 1
    assert usage_events[0].success_flag is False
    assert usage_events[0].proxy_api_key_id == seed.proxy_api_key_id
    assert usage_events[0].proxy_api_key_name_snapshot == seed.proxy_api_key_name


@pytest.mark.asyncio
async def test_streaming_success_finalization_writes_one_final_usage_event() -> None:
    seed = await _seed_runtime_route(connection_count=1)
    raw_body = json.dumps(
        {
            "model": seed.model_id,
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        }
    ).encode("utf-8")
    stream_chunk = b'data: {"usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}\n\n'
    upstream_response = CompletedStreamResponse([stream_chunk])
    app = FastAPI()
    app.state.http_client = StreamingHttpClient(upstream_response)
    request = _build_request(
        app=app,
        path="/v1/responses",
        proxy_api_key_id=seed.proxy_api_key_id,
        proxy_api_key_name=seed.proxy_api_key_name,
    )

    async with AsyncSessionLocal() as db:
        response = await _handle_proxy(
            request=request,
            db=db,
            raw_body=raw_body,
            request_path="/v1/responses",
            profile_id=seed.profile_id,
        )

    assert isinstance(response, StreamingResponse)
    streamed_body = await _consume_streaming_response(response)
    request_logs = await _load_request_logs(seed.profile_id)
    usage_events = await _load_usage_events(seed.profile_id)

    assert streamed_body == stream_chunk
    assert upstream_response.closed is True
    assert len(request_logs) == 1
    assert request_logs[0].is_stream is True
    assert request_logs[0].proxy_api_key_id == seed.proxy_api_key_id
    assert request_logs[0].proxy_api_key_name_snapshot == seed.proxy_api_key_name
    assert len(usage_events) == 1
    assert usage_events[0].status_code == 200
    assert usage_events[0].attempt_count == 1
    assert usage_events[0].success_flag is True
    assert usage_events[0].total_tokens == 5
    assert usage_events[0].proxy_api_key_id == seed.proxy_api_key_id
    assert usage_events[0].proxy_api_key_name_snapshot == seed.proxy_api_key_name


@pytest.mark.asyncio
async def test_exhausted_failover_502_writes_one_final_usage_event_from_last_attempt() -> (
    None
):
    seed = await _seed_runtime_route(connection_count=2)
    raw_body = json.dumps(
        {"model": seed.model_id, "messages": [{"role": "user", "content": "hi"}]}
    ).encode("utf-8")
    app = FastAPI()
    app.state.http_client = BufferedHttpClient(
        [
            _json_response(500, {"error": {"message": "retry-one"}}),
            _json_response(500, {"error": {"message": "retry-two"}}),
        ]
    )
    request = _build_request(
        app=app,
        path="/v1/chat/completions",
        proxy_api_key_id=seed.proxy_api_key_id,
        proxy_api_key_name=seed.proxy_api_key_name,
    )

    async with AsyncSessionLocal() as db:
        with pytest.raises(HTTPException) as exc_info:
            await _handle_proxy(
                request=request,
                db=db,
                raw_body=raw_body,
                request_path="/v1/chat/completions",
                profile_id=seed.profile_id,
            )

    request_logs = await _load_request_logs(seed.profile_id)
    usage_events = await _load_usage_events(seed.profile_id)

    assert exc_info.value.status_code == 502
    assert [entry.attempt_number for entry in request_logs] == [1, 2]
    assert len(usage_events) == 1
    assert usage_events[0].status_code == 502
    assert usage_events[0].attempt_count == 2
    assert usage_events[0].connection_id == seed.connection_ids[1]
    assert usage_events[0].endpoint_id == seed.endpoint_ids[1]
    assert usage_events[0].success_flag is False
    assert usage_events[0].proxy_api_key_id == seed.proxy_api_key_id
    assert usage_events[0].proxy_api_key_name_snapshot == seed.proxy_api_key_name


@pytest.mark.asyncio
async def test_runtime_zero_attempt_503_writes_no_usage_event() -> None:
    seed = await _seed_runtime_route(connection_count=1)
    raw_body = json.dumps(
        {"model": seed.model_id, "messages": [{"role": "user", "content": "hi"}]}
    ).encode("utf-8")
    app = FastAPI()
    app.state.http_client = BufferedHttpClient(
        [_json_response(200, {"id": "unused", "usage": {"total_tokens": 1}})]
    )
    request = _build_request(
        app=app,
        path="/v1/chat/completions",
        proxy_api_key_id=seed.proxy_api_key_id,
        proxy_api_key_name=seed.proxy_api_key_name,
    )

    async with AsyncSessionLocal() as db:
        with patch(
            "app.routers.proxy._endpoint_is_active_now", AsyncMock(return_value=False)
        ):
            with pytest.raises(HTTPException) as exc_info:
                await _handle_proxy(
                    request=request,
                    db=db,
                    raw_body=raw_body,
                    request_path="/v1/chat/completions",
                    profile_id=seed.profile_id,
                )

    assert exc_info.value.status_code == 503
    assert await _load_request_logs(seed.profile_id) == []
    assert await _load_usage_events(seed.profile_id) == []


@pytest.mark.asyncio
async def test_pre_setup_zero_attempt_503_writes_no_usage_event() -> None:
    seed = await _seed_runtime_route(connection_count=0)
    raw_body = json.dumps(
        {"model": seed.model_id, "messages": [{"role": "user", "content": "hi"}]}
    ).encode("utf-8")
    app = FastAPI()
    app.state.http_client = BufferedHttpClient([])
    request = _build_request(
        app=app,
        path="/v1/chat/completions",
        proxy_api_key_id=seed.proxy_api_key_id,
        proxy_api_key_name=seed.proxy_api_key_name,
    )

    async with AsyncSessionLocal() as db:
        with pytest.raises(HTTPException) as exc_info:
            await _handle_proxy(
                request=request,
                db=db,
                raw_body=raw_body,
                request_path="/v1/chat/completions",
                profile_id=seed.profile_id,
            )

    assert exc_info.value.status_code == 503
    assert await _load_request_logs(seed.profile_id) == []
    assert await _load_usage_events(seed.profile_id) == []
