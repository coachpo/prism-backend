import pytest
from httpx import ASGITransport, AsyncClient
from typing import cast

from app.main import app


class TestDEF087_HealthEndpointContract:
    @pytest.mark.asyncio
    async def test_health_returns_ok_status_and_version_string(self):
        transport = ASGITransport(app=app)

        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.get("/health")

        assert response.status_code == 200
        payload = cast(dict[str, object], response.json())

        assert payload["status"] == "ok"
        assert isinstance(payload["version"], str)
        assert payload["version"]
