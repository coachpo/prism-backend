from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.core.config import get_settings
from app.core.crypto import hash_password
from app.core.database import AsyncSessionLocal, get_engine
from app.core.time import utc_now
from app.main import app
from app.models.models import PasswordResetChallenge, ProxyApiKey, RefreshToken
from app.services.auth_service import get_or_create_app_auth_settings
from app.services.profile_invariants import ensure_profile_invariants


TEST_USERNAME = "admin"
TEST_PASSWORD = "11111111"
UPDATED_PASSWORD = "22222222"
TEST_EMAIL = "admin@example.com"


async def _reset_auth_state() -> int:
    await get_engine().dispose()
    async with AsyncSessionLocal() as session:
        profile = await ensure_profile_invariants(session)
        settings_row = await get_or_create_app_auth_settings(session)
        settings_row.auth_enabled = True
        settings_row.username = TEST_USERNAME
        settings_row.email = TEST_EMAIL
        settings_row.pending_email = None
        settings_row.email_bound_at = utc_now()
        settings_row.password_hash = hash_password(TEST_PASSWORD)
        settings_row.token_version = 0
        settings_row.last_login_at = None
        settings_row.email_verification_code_hash = None
        settings_row.email_verification_expires_at = None
        settings_row.email_verification_attempt_count = 0
        await session.execute(delete(RefreshToken))
        await session.execute(delete(PasswordResetChallenge))
        await session.execute(delete(ProxyApiKey))
        await session.commit()
        return profile.id


async def _cleanup_auth_state() -> None:
    async with AsyncSessionLocal() as session:
        settings_row = await get_or_create_app_auth_settings(session)
        settings_row.auth_enabled = False
        settings_row.username = None
        settings_row.email = None
        settings_row.pending_email = None
        settings_row.email_bound_at = None
        settings_row.password_hash = None
        settings_row.token_version = 0
        settings_row.last_login_at = None
        settings_row.email_verification_code_hash = None
        settings_row.email_verification_expires_at = None
        settings_row.email_verification_attempt_count = 0
        await session.execute(delete(RefreshToken))
        await session.execute(delete(PasswordResetChallenge))
        await session.execute(delete(ProxyApiKey))
        await session.commit()
    await get_engine().dispose()


async def _login(
    client: AsyncClient, password: str = TEST_PASSWORD
) -> tuple[str | None, str | None]:
    response = await client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "password": password},
    )
    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    return (
        response.cookies.get(get_settings().auth_cookie_name),
        response.cookies.get(get_settings().auth_refresh_cookie_name),
    )


class TestDEF069_AuthSessionLifecycle:
    @pytest.mark.asyncio
    async def test_login_refresh_logout_cycle_rotates_and_revokes_session(self):
        await _reset_auth_state()
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                unauthenticated = await client.get("/api/settings/auth")
                assert unauthenticated.status_code == 401

                access_cookie, refresh_cookie = await _login(client)
                assert access_cookie
                assert refresh_cookie

                session_response = await client.get("/api/auth/session")
                assert session_response.status_code == 200
                assert session_response.json() == {
                    "authenticated": True,
                    "auth_enabled": True,
                    "username": TEST_USERNAME,
                }

                refresh_response = await client.post("/api/auth/refresh")
                assert refresh_response.status_code == 200
                assert refresh_response.json()["authenticated"] is True
                rotated_refresh_cookie = refresh_response.cookies.get(
                    get_settings().auth_refresh_cookie_name
                )
                assert rotated_refresh_cookie
                assert rotated_refresh_cookie != refresh_cookie

                logout_response = await client.post("/api/auth/logout")
                assert logout_response.status_code == 200
                assert logout_response.json() == {
                    "authenticated": False,
                    "auth_enabled": True,
                    "username": None,
                }

                refresh_after_logout = await client.post("/api/auth/refresh")
                assert refresh_after_logout.status_code == 200
                assert refresh_after_logout.json() == {
                    "authenticated": False,
                    "auth_enabled": True,
                    "username": None,
                }
        finally:
            await _cleanup_auth_state()


class TestDEF070_PasswordResetInvalidatesSessions:
    @pytest.mark.asyncio
    async def test_password_reset_request_and_confirm_revoke_existing_sessions(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        await _reset_auth_state()
        transport = ASGITransport(app=app)
        captured_otp: dict[str, str] = {}

        def capture_password_reset_email(*, recipient: str, otp_code: str) -> None:
            captured_otp["recipient"] = recipient
            captured_otp["otp_code"] = otp_code

        monkeypatch.setattr(
            "app.routers.auth.send_password_reset_email",
            capture_password_reset_email,
        )
        try:
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                _, refresh_cookie = await _login(client)
                assert refresh_cookie

                request_response = await client.post(
                    "/api/auth/password-reset/request",
                    json={"username_or_email": TEST_USERNAME},
                )
                assert request_response.status_code == 200
                assert request_response.json() == {"success": True}

                async with AsyncSessionLocal() as session:
                    challenge_count = await session.scalar(
                        select(func.count(PasswordResetChallenge.id))
                    )
                    assert challenge_count == 1
                assert captured_otp.get("recipient") == TEST_EMAIL
                assert captured_otp.get("otp_code")

                confirm_response = await client.post(
                    "/api/auth/password-reset/confirm",
                    json={
                        "otp_code": captured_otp["otp_code"],
                        "new_password": UPDATED_PASSWORD,
                    },
                )
                assert confirm_response.status_code == 200
                assert confirm_response.json() == {"success": True}

            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as stale_session_client:
                stale_session_client.cookies.set(
                    get_settings().auth_refresh_cookie_name,
                    refresh_cookie,
                )
                refresh_after_reset = await stale_session_client.post(
                    "/api/auth/refresh"
                )
                assert refresh_after_reset.status_code == 200
                assert refresh_after_reset.json() == {
                    "authenticated": False,
                    "auth_enabled": True,
                    "username": None,
                }

            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                login_with_new_password = await _login(
                    client, password=UPDATED_PASSWORD
                )
                assert login_with_new_password[0]
        finally:
            await _cleanup_auth_state()


class TestDEF071_ProxyApiKeyHeaderAcceptance:
    @pytest.mark.asyncio
    async def test_proxy_api_key_accepts_all_supported_header_forms(self):
        await _reset_auth_state()
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                await _login(client)
                create_response = await client.post(
                    "/api/settings/auth/proxy-keys",
                    json={"name": f"DEF071 {uuid4().hex[:8]}"},
                )
                assert create_response.status_code == 201
                raw_key = create_response.json()["key"]
                assert raw_key.startswith("pm-")

            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as proxy_client:
                missing_key_response = await proxy_client.post(
                    "/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}]},
                )
                assert missing_key_response.status_code == 401
                assert missing_key_response.json() == {
                    "detail": "Proxy API key required"
                }

                invalid_key_response = await proxy_client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer pm-invalid-key"},
                    json={"messages": [{"role": "user", "content": "hi"}]},
                )
                assert invalid_key_response.status_code == 401
                assert invalid_key_response.json() == {
                    "detail": "Invalid proxy API key"
                }

                header_sets = [
                    {"Authorization": f"Bearer {raw_key}"},
                    {"x-api-key": raw_key},
                    {"x-goog-api-key": raw_key},
                ]
                for headers in header_sets:
                    response = await proxy_client.post(
                        "/v1/chat/completions",
                        headers=headers,
                        json={"messages": [{"role": "user", "content": "hi"}]},
                    )
                    assert response.status_code == 400
                    assert response.json()["detail"].startswith(
                        "Cannot determine model"
                    )
        finally:
            await _cleanup_auth_state()


class TestDEF072_SecretSanitization:
    @pytest.mark.asyncio
    async def test_endpoint_responses_and_exports_never_return_raw_api_keys(self):
        profile_id = await _reset_auth_state()
        transport = ASGITransport(app=app)
        endpoint_name = f"DEF072 endpoint {uuid4().hex[:8]}"
        raw_secret = "sk-test-super-secret"
        profile_headers = {"X-Profile-Id": str(profile_id)}

        try:
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                await _login(client)

                create_response = await client.post(
                    "/api/endpoints",
                    headers=profile_headers,
                    json={
                        "name": endpoint_name,
                        "base_url": "https://example.com/v1",
                        "api_key": raw_secret,
                    },
                )
                assert create_response.status_code == 201
                created = create_response.json()
                assert "api_key" not in created
                assert created["has_api_key"] is True
                assert created["masked_api_key"]
                assert raw_secret not in created["masked_api_key"]

                list_response = await client.get(
                    "/api/endpoints", headers=profile_headers
                )
                assert list_response.status_code == 200
                matching_endpoint = next(
                    item
                    for item in list_response.json()
                    if item["name"] == endpoint_name
                )
                assert "api_key" not in matching_endpoint
                assert matching_endpoint["has_api_key"] is True
                assert matching_endpoint["masked_api_key"]
                assert raw_secret not in matching_endpoint["masked_api_key"]

                export_response = await client.get(
                    "/api/config/export", headers=profile_headers
                )
                assert export_response.status_code == 200
                export_endpoint = next(
                    item
                    for item in export_response.json()["endpoints"]
                    if item["name"] == endpoint_name
                )
                assert export_endpoint["api_key"] == ""
        finally:
            await _cleanup_auth_state()
