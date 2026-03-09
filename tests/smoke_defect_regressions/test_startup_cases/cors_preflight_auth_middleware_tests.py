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
