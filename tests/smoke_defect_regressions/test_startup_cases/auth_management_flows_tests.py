from __future__ import annotations

from http.cookies import SimpleCookie
import socket
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi import WebSocketDisconnect
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.core.auth import create_access_token
from app.core.config import get_settings
from app.core.crypto import hash_password
from app.core.crypto import hash_opaque_token
from app.core.database import AsyncSessionLocal, get_engine
from app.core.time import utc_now
from app.main import app
from app.models.models import (
    Connection,
    Endpoint,
    ModelConfig,
    PasswordResetChallenge,
    Provider,
    ProxyApiKey,
    RefreshToken,
)
from app.routers.realtime import websocket_endpoint
from app.services.auth_service import (
    get_or_create_app_auth_settings,
    send_password_reset_email,
)
from app.services.profile_invariants import ensure_profile_invariants


TEST_USERNAME = "admin"
TEST_PASSWORD = "11111111"
UPDATED_PASSWORD = "22222222"
TEST_EMAIL = "admin@example.com"


class _FakeWebSocket:
    def __init__(self, *, cookies: dict[str, str]):
        self.cookies = cookies
        self.accepted = False
        self.close_code: int | None = None
        self.sent_messages: list[dict[str, object]] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.close_code = code

    async def send_json(self, data: dict[str, object]) -> None:
        self.sent_messages.append(data)

    async def receive_json(self) -> dict[str, object]:
        raise WebSocketDisconnect()


def _get_cookie_max_age(headers: list[str], cookie_name: str) -> int | None:
    for header in headers:
        cookie = SimpleCookie()
        cookie.load(header)
        morsel = cookie.get(cookie_name)
        if morsel is None:
            continue
        max_age = morsel["max-age"]
        return int(max_age) if max_age else None
    return None


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
    client: AsyncClient,
    password: str = TEST_PASSWORD,
    session_duration: str = "7_days",
) -> tuple[str | None, str | None]:
    response = await client.post(
        "/api/auth/login",
        json={
            "username": TEST_USERNAME,
            "password": password,
            "session_duration": session_duration,
        },
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

    @pytest.mark.asyncio
    async def test_public_bootstrap_reports_authenticated_session_and_recovers_from_refresh_cookie(
        self,
    ):
        await _reset_auth_state()
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                await _login(client)

                bootstrap_response = await client.get("/api/auth/public-bootstrap")
                assert bootstrap_response.status_code == 200
                assert bootstrap_response.json() == {
                    "authenticated": True,
                    "auth_enabled": True,
                    "username": TEST_USERNAME,
                }

                client.cookies.delete(get_settings().auth_cookie_name)

                refresh_recovery = await client.get("/api/auth/public-bootstrap")
                assert refresh_recovery.status_code == 200
                assert refresh_recovery.json() == {
                    "authenticated": True,
                    "auth_enabled": True,
                    "username": TEST_USERNAME,
                }
                assert (
                    refresh_recovery.cookies.get(get_settings().auth_cookie_name)
                    is not None
                )
        finally:
            await _cleanup_auth_state()

    @pytest.mark.asyncio
    async def test_public_bootstrap_returns_auth_disabled_when_management_auth_is_off(
        self,
    ):
        await _reset_auth_state()
        transport = ASGITransport(app=app)
        try:
            async with AsyncSessionLocal() as session:
                settings_row = await get_or_create_app_auth_settings(session)
                settings_row.auth_enabled = False
                await session.commit()

            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.get("/api/auth/public-bootstrap")
                assert response.status_code == 200
                assert response.json() == {
                    "authenticated": False,
                    "auth_enabled": False,
                    "username": None,
                }
        finally:
            await _cleanup_auth_state()

    @pytest.mark.asyncio
    async def test_login_session_duration_controls_cookie_persistence_and_refresh_rotation(
        self,
    ):
        await _reset_auth_state()
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                session_login = await client.post(
                    "/api/auth/login",
                    json={
                        "username": TEST_USERNAME,
                        "password": TEST_PASSWORD,
                        "session_duration": "session",
                    },
                )
                assert session_login.status_code == 200
                session_set_cookie_headers = session_login.headers.get_list(
                    "set-cookie"
                )
                assert (
                    _get_cookie_max_age(
                        session_set_cookie_headers,
                        get_settings().auth_refresh_cookie_name,
                    )
                    is None
                )

                session_refresh = await client.post("/api/auth/refresh")
                assert session_refresh.status_code == 200
                rotated_session_headers = session_refresh.headers.get_list("set-cookie")
                assert (
                    _get_cookie_max_age(
                        rotated_session_headers,
                        get_settings().auth_refresh_cookie_name,
                    )
                    is None
                )

                await client.post("/api/auth/logout")

                default_login = await client.post(
                    "/api/auth/login",
                    json={
                        "username": TEST_USERNAME,
                        "password": TEST_PASSWORD,
                    },
                )
                assert default_login.status_code == 200
                default_headers = default_login.headers.get_list("set-cookie")
                default_max_age = _get_cookie_max_age(
                    default_headers,
                    get_settings().auth_refresh_cookie_name,
                )
                assert default_max_age is not None
                assert 604_700 <= default_max_age <= 604_800

                default_refresh = await client.post("/api/auth/refresh")
                assert default_refresh.status_code == 200

                await client.post("/api/auth/logout")

                remembered_login = await client.post(
                    "/api/auth/login",
                    json={
                        "username": TEST_USERNAME,
                        "password": TEST_PASSWORD,
                        "session_duration": "30_days",
                    },
                )
                assert remembered_login.status_code == 200
                remembered_headers = remembered_login.headers.get_list("set-cookie")
                remembered_max_age = _get_cookie_max_age(
                    remembered_headers,
                    get_settings().auth_refresh_cookie_name,
                )
                assert remembered_max_age is not None
                assert 2_591_900 <= remembered_max_age <= 2_592_000

                remembered_refresh = await client.post("/api/auth/refresh")
                assert remembered_refresh.status_code == 200
                rotated_remembered_headers = remembered_refresh.headers.get_list(
                    "set-cookie"
                )
                assert any(
                    get_settings().auth_refresh_cookie_name in header
                    and "Max-Age=" in header
                    for header in rotated_remembered_headers
                )

            async with AsyncSessionLocal() as session:
                refresh_tokens = list(
                    (
                        await session.execute(
                            select(RefreshToken).order_by(RefreshToken.id.asc())
                        )
                    )
                    .scalars()
                    .all()
                )
                assert refresh_tokens[0].expires_at == refresh_tokens[1].expires_at
                assert refresh_tokens[2].expires_at == refresh_tokens[3].expires_at
                assert refresh_tokens[4].expires_at == refresh_tokens[5].expires_at
                assert [token.session_duration for token in refresh_tokens] == [
                    "session",
                    "session",
                    "7_days",
                    "7_days",
                    "30_days",
                    "30_days",
                ]
        finally:
            await _cleanup_auth_state()

    @pytest.mark.asyncio
    async def test_revoked_token_replay_revokes_only_matching_refresh_family(self):
        await _reset_auth_state()
        transport = ASGITransport(app=app)
        try:
            async with (
                AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as family_client,
                AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as other_client,
            ):
                _, stale_family_refresh_cookie = await _login(family_client)
                assert stale_family_refresh_cookie

                family_refresh = await family_client.post("/api/auth/refresh")
                assert family_refresh.status_code == 200
                active_family_refresh_cookie = family_refresh.cookies.get(
                    get_settings().auth_refresh_cookie_name
                )
                assert active_family_refresh_cookie
                assert active_family_refresh_cookie != stale_family_refresh_cookie

                _, other_refresh_cookie = await _login(other_client)
                assert other_refresh_cookie

                async with AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as replay_client:
                    replay_client.cookies.set(
                        get_settings().auth_refresh_cookie_name,
                        stale_family_refresh_cookie,
                    )
                    replay_response = await replay_client.post("/api/auth/refresh")
                    assert replay_response.status_code == 200
                    assert replay_response.json() == {
                        "authenticated": False,
                        "auth_enabled": True,
                        "username": None,
                    }

                family_client.cookies.set(
                    get_settings().auth_refresh_cookie_name,
                    active_family_refresh_cookie,
                )
                family_after_replay = await family_client.post("/api/auth/refresh")
                assert family_after_replay.status_code == 200
                assert family_after_replay.json() == {
                    "authenticated": False,
                    "auth_enabled": True,
                    "username": None,
                }

                other_after_replay = await other_client.post("/api/auth/refresh")
                assert other_after_replay.status_code == 200
                assert other_after_replay.json()["authenticated"] is True
        finally:
            await _cleanup_auth_state()

    @pytest.mark.asyncio
    async def test_refresh_fails_closed_when_persisted_session_duration_is_invalid(
        self,
    ):
        await _reset_auth_state()
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                _, refresh_cookie = await _login(client)
                assert refresh_cookie

                refresh_hash = hash_opaque_token(refresh_cookie)
                async with AsyncSessionLocal() as session:
                    refresh_row = (
                        await session.execute(
                            select(RefreshToken)
                            .where(RefreshToken.token_hash == refresh_hash)
                            .limit(1)
                        )
                    ).scalar_one()
                    refresh_row.session_duration = "invalid"
                    await session.commit()

                refresh_response = await client.post("/api/auth/refresh")
                assert refresh_response.status_code == 200
                assert refresh_response.json() == {
                    "authenticated": False,
                    "auth_enabled": True,
                    "username": None,
                }

                async with AsyncSessionLocal() as session:
                    stored_refresh_row = (
                        await session.execute(
                            select(RefreshToken)
                            .where(RefreshToken.token_hash == refresh_hash)
                            .limit(1)
                        )
                    ).scalar_one()
                    assert stored_refresh_row.revoked_at is not None
                    refresh_token_count = await session.scalar(
                        select(func.count(RefreshToken.id))
                    )
                    assert refresh_token_count == 1
        finally:
            await _cleanup_auth_state()

    @pytest.mark.asyncio
    async def test_realtime_websocket_uses_configured_access_cookie_name(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        await _reset_auth_state()
        custom_cookie_name = f"custom_access_token_{uuid4().hex[:8]}"
        monkeypatch.setenv("AUTH_COOKIE_NAME", custom_cookie_name)
        get_settings.cache_clear()

        try:
            async with AsyncSessionLocal() as session:
                settings_row = await get_or_create_app_auth_settings(session)
                access_token = create_access_token(
                    subject_id=settings_row.id,
                    username=settings_row.username or "",
                    token_version=settings_row.token_version,
                )

            websocket = _FakeWebSocket(cookies={custom_cookie_name: access_token})

            async with AsyncSessionLocal() as session:
                await websocket_endpoint(websocket, session)

            assert websocket.accepted is True
            assert websocket.close_code is None
            assert websocket.sent_messages[:2] == [
                {
                    "type": "authenticated",
                    "username": TEST_USERNAME,
                },
                {
                    "type": "heartbeat",
                },
            ]
        finally:
            get_settings.cache_clear()
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

    def test_connection_response_serializes_unreadable_endpoint_secret(self):
        from app.schemas.schemas import ConnectionResponse

        now = utc_now()
        endpoint = Endpoint(
            id=31,
            profile_id=1,
            name="broken-secret-endpoint",
            base_url="https://example.com/v1",
            api_key="enc:not-a-valid-token",
            position=0,
            created_at=now,
            updated_at=now,
        )
        connection = Connection(
            id=41,
            profile_id=1,
            model_config_id=11,
            endpoint_id=31,
            endpoint_rel=endpoint,
            is_active=True,
            priority=0,
            name="primary",
            auth_type=None,
            custom_headers=None,
            pricing_template_id=None,
            pricing_template_rel=None,
            health_status="unknown",
            health_detail=None,
            last_health_check=None,
            created_at=now,
            updated_at=now,
        )

        response = ConnectionResponse.model_validate(connection, from_attributes=True)

        assert response.endpoint is not None
        assert response.endpoint.has_api_key is True
        assert response.endpoint.masked_api_key == "********"

    @pytest.mark.asyncio
    async def test_list_connections_route_serializes_unreadable_endpoint_secret(self):
        profile_id = await _reset_auth_state()
        transport = ASGITransport(app=app)
        suffix = uuid4().hex[:8]

        try:
            async with AsyncSessionLocal() as session:
                provider = (
                    await session.execute(
                        select(Provider)
                        .where(Provider.provider_type == "openai")
                        .order_by(Provider.id.asc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if provider is None:
                    provider = Provider(
                        name=f"DEF072 OpenAI {suffix}",
                        provider_type="openai",
                    )
                    session.add(provider)
                    await session.flush()

                model = ModelConfig(
                    profile_id=profile_id,
                    provider_id=provider.id,
                    model_id=f"def072-model-{suffix}",
                    model_type="native",
                    lb_strategy="single",
                    failover_recovery_enabled=True,
                    failover_recovery_cooldown_seconds=60,
                    is_enabled=True,
                )
                session.add(model)
                await session.flush()

                endpoint = Endpoint(
                    profile_id=profile_id,
                    name=f"DEF072 broken secret {suffix}",
                    base_url="https://example.com/v1",
                    api_key="enc:not-a-valid-token",
                    position=0,
                )
                session.add(endpoint)
                await session.flush()

                connection = Connection(
                    profile_id=profile_id,
                    model_config_id=model.id,
                    endpoint_id=endpoint.id,
                    is_active=True,
                    priority=0,
                    name=f"DEF072 connection {suffix}",
                )
                session.add(connection)
                await session.commit()

            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                await _login(client)
                response = await client.get(
                    f"/api/models/{model.id}/connections",
                    headers={"X-Profile-Id": str(profile_id)},
                )

            assert response.status_code == 200
            payload = response.json()
            assert len(payload) == 1
            assert payload[0]["endpoint"]["has_api_key"] is True
            assert payload[0]["endpoint"]["masked_api_key"] == "********"
        finally:
            await _cleanup_auth_state()

    @pytest.mark.asyncio
    async def test_endpoint_update_recovers_from_unreadable_stored_secret(self):
        profile_id = await _reset_auth_state()
        transport = ASGITransport(app=app)
        endpoint_name = f"DEF072 recovery endpoint {uuid4().hex[:8]}"
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
                        "api_key": "sk-initial-secret",
                    },
                )
                assert create_response.status_code == 201
                endpoint_id = create_response.json()["id"]

                async with AsyncSessionLocal() as session:
                    endpoint = await session.get(Endpoint, endpoint_id)
                    assert endpoint is not None
                    endpoint.api_key = "enc:not-a-valid-token"
                    await session.commit()

                update_response = await client.put(
                    f"/api/endpoints/{endpoint_id}",
                    headers=profile_headers,
                    json={"api_key": "sk-recovered-secret"},
                )

                assert update_response.status_code == 200
                updated = update_response.json()
                assert updated["has_api_key"] is True
                assert updated["masked_api_key"]
                assert "sk-recovered-secret" not in updated["masked_api_key"]
        finally:
            await _cleanup_auth_state()


class TestDEF073_AuthEmailDeliveryFailures:
    @pytest.mark.asyncio
    async def test_password_reset_request_still_succeeds_when_email_send_raises_503(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        await _reset_auth_state()
        transport = ASGITransport(app=app)

        def fail_password_reset_email(*, recipient: str, otp_code: str) -> None:
            raise HTTPException(
                status_code=503, detail="Email service temporarily unavailable"
            )

        monkeypatch.setattr(
            "app.routers.auth.send_password_reset_email",
            fail_password_reset_email,
        )

        try:
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.post(
                    "/api/auth/password-reset/request",
                    json={"username_or_email": TEST_USERNAME},
                )

            assert response.status_code == 200
            assert response.json() == {"success": True}
        finally:
            await _cleanup_auth_state()

    @pytest.mark.asyncio
    async def test_email_verification_request_returns_503_when_smtp_lookup_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        await _reset_auth_state()
        transport = ASGITransport(app=app)
        settings = get_settings()
        original_smtp_host = settings.smtp_host
        original_sender_email = settings.smtp_sender_email
        settings.smtp_host = "smtp.example.invalid"
        settings.smtp_sender_email = "prism@example.com"

        def fail_smtp(*args, **kwargs):
            raise socket.gaierror(8, "nodename nor servname provided, or not known")

        monkeypatch.setattr("app.services.auth_service.smtplib.SMTP", fail_smtp)

        try:
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                await _login(client)
                response = await client.post(
                    "/api/settings/auth/email-verification/request",
                    json={"email": "new-admin@example.com"},
                )

            assert response.status_code == 503
            assert response.json() == {
                "detail": "Email service temporarily unavailable"
            }
        finally:
            settings.smtp_host = original_smtp_host
            settings.smtp_sender_email = original_sender_email
            await _cleanup_auth_state()

    def test_password_reset_email_raises_503_when_smtp_lookup_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        settings = get_settings()
        original_smtp_host = settings.smtp_host
        original_sender_email = settings.smtp_sender_email
        settings.smtp_host = "smtp.example.invalid"
        settings.smtp_sender_email = "prism@example.com"

        def fail_smtp(*args, **kwargs):
            raise socket.gaierror(8, "nodename nor servname provided, or not known")

        monkeypatch.setattr("app.services.auth_service.smtplib.SMTP", fail_smtp)

        try:
            with pytest.raises(HTTPException) as exc_info:
                send_password_reset_email(
                    recipient=TEST_EMAIL,
                    otp_code="123456",
                )

            assert exc_info.value.status_code == 503
            assert exc_info.value.detail == "Email service temporarily unavailable"
        finally:
            settings.smtp_host = original_smtp_host
            settings.smtp_sender_email = original_sender_email
