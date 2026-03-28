import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from app.routers.proxy_domains.request_setup import ProxyRoutingRejection


def _build_request(*, app: FastAPI) -> Request:
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


class TestDEF087_ProxyUnroutableTargetRejection:
    @pytest.mark.asyncio
    async def test_handle_proxy_logs_empty_target_proxy_rejection_once(self):
        from app.routers.proxy import _handle_proxy

        app = FastAPI()
        app.state.http_client = object()
        request = _build_request(app=app)
        raw_body = json.dumps(
            {"model": "empty-proxy", "messages": [{"role": "user", "content": "hi"}]}
        ).encode("utf-8")
        rejection = ProxyRoutingRejection(
            api_family="openai",
            detail="Proxy model 'empty-proxy' has no routable targets.",
            ingress_request_id="req-empty-proxy",
            is_streaming=False,
            model_id="empty-proxy",
            proxy_api_key_id=None,
            proxy_api_key_name=None,
            vendor_id=7,
            vendor_key="openrouter",
            vendor_name="OpenRouter",
        )
        log_request = AsyncMock(return_value=901)

        with (
            patch(
                "app.routers.proxy.prepare_proxy_request",
                AsyncMock(side_effect=rejection),
            ),
            patch("app.routers.proxy.log_request", log_request),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await _handle_proxy(
                    request=request,
                    db=AsyncMock(),
                    raw_body=raw_body,
                    request_path="/v1/chat/completions",
                    profile_id=1,
                )

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == rejection.detail
        log_request.assert_awaited_once()
        log_kwargs = log_request.await_args.kwargs
        assert log_kwargs["model_id"] == "empty-proxy"
        assert log_kwargs["resolved_target_model_id"] is None
        assert log_kwargs["endpoint_id"] is None
        assert log_kwargs["connection_id"] is None
        assert log_kwargs["status_code"] == 503
        assert log_kwargs["request_path"] == "/v1/chat/completions"
        assert log_kwargs["error_detail"] == rejection.detail

    @pytest.mark.asyncio
    async def test_handle_proxy_logs_unroutable_target_rejection_with_proxy_key_metadata_once(
        self,
    ):
        from app.routers.proxy import _handle_proxy

        app = FastAPI()
        app.state.http_client = object()
        request = _build_request(app=app)
        raw_body = json.dumps(
            {
                "model": "no-plan-proxy",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            }
        ).encode("utf-8")
        rejection = ProxyRoutingRejection(
            api_family="openai",
            detail="Proxy model 'no-plan-proxy' has no routable targets.",
            ingress_request_id="req-no-plan-proxy",
            is_streaming=True,
            model_id="no-plan-proxy",
            proxy_api_key_id=42,
            proxy_api_key_name="primary-proxy-key",
            vendor_id=8,
            vendor_key="openrouter",
            vendor_name="OpenRouter",
        )
        log_request = AsyncMock(return_value=902)

        with (
            patch(
                "app.routers.proxy.prepare_proxy_request",
                AsyncMock(side_effect=rejection),
            ),
            patch("app.routers.proxy.log_request", log_request),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await _handle_proxy(
                    request=request,
                    db=AsyncMock(),
                    raw_body=raw_body,
                    request_path="/v1/chat/completions",
                    profile_id=1,
                )

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == rejection.detail
        log_request.assert_awaited_once()
        log_kwargs = log_request.await_args.kwargs
        assert log_kwargs["model_id"] == "no-plan-proxy"
        assert log_kwargs["proxy_api_key_id"] == 42
        assert log_kwargs["proxy_api_key_name_snapshot"] == "primary-proxy-key"
        assert log_kwargs["ingress_request_id"] == "req-no-plan-proxy"
        assert log_kwargs["is_stream"] is True
        assert log_kwargs["success_flag"] is False
