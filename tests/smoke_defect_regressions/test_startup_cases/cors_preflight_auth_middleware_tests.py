import pytest
from httpx import ASGITransport, AsyncClient

from app.core.database import AsyncSessionLocal, get_engine
from app.main import app
from app.services.auth_service import get_or_create_app_auth_settings


async def _set_auth_enabled(value: bool) -> None:
    await get_engine().dispose()
    async with AsyncSessionLocal() as session:
        settings_row = await get_or_create_app_auth_settings(session)
        settings_row.auth_enabled = value
        await session.commit()


class TestDEF068_CorsPreflightBypassesAuthMiddleware:
    @pytest.mark.asyncio
    async def test_options_preflight_to_authenticated_api_returns_cors_headers(self):
        await _set_auth_enabled(True)
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.options(
                    "/api/settings/auth",
                    headers={
                        "Origin": "http://127.0.0.1:5173",
                        "Access-Control-Request-Method": "GET",
                    },
                )

            assert response.status_code == 200
            assert (
                response.headers.get("access-control-allow-origin")
                == "http://127.0.0.1:5173"
            )
        finally:
            await _set_auth_enabled(False)
            await get_engine().dispose()

    @pytest.mark.asyncio
    async def test_unauthenticated_management_error_keeps_cors_headers(self):
        await _set_auth_enabled(True)
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.get(
                    "/api/settings/auth",
                    headers={"Origin": "http://127.0.0.1:5173"},
                )

            assert response.status_code == 401
            assert (
                response.headers.get("access-control-allow-origin")
                == "http://127.0.0.1:5173"
            )
        finally:
            await _set_auth_enabled(False)
            await get_engine().dispose()


class TestDEF073_ProxyCorsWildcardOrigins:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("path", "origin"),
        [
            ("/v1/chat/completions", "https://appassets.androidplatform.net"),
            (
                "/v1beta/models/gemini-3.1-pro-preview:generateContent",
                "capacitor://localhost",
            ),
            ("/v1/chat/completions", "null"),
        ],
    )
    async def test_proxy_preflight_allows_mobile_origins(self, path: str, origin: str):
        transport = ASGITransport(app=app)

        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.options(
                path,
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization,content-type",
                },
            )

        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == origin
        assert response.headers.get("access-control-allow-credentials") == "true"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "origin",
        [
            "https://appassets.androidplatform.net",
            "capacitor://localhost",
            "null",
        ],
    )
    async def test_public_management_response_reflects_mobile_origin(
        self, origin: str
    ):
        transport = ASGITransport(app=app)

        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get(
                "/api/auth/status",
                headers={"Origin": origin},
            )

        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == origin
        assert response.headers.get("access-control-allow-credentials") == "true"

    @pytest.mark.asyncio
    async def test_unauthenticated_management_error_keeps_cors_headers_for_mobile_origin(
        self,
    ):
        await _set_auth_enabled(True)
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.get(
                    "/api/settings/auth",
                    headers={"Origin": "https://appassets.androidplatform.net"},
                )

            assert response.status_code == 401
            assert (
                response.headers.get("access-control-allow-origin")
                == "https://appassets.androidplatform.net"
            )
        finally:
            await _set_auth_enabled(False)
            await get_engine().dispose()
