from __future__ import annotations

import asyncio
from collections.abc import Iterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event

from app.core.auth import create_access_token
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, get_engine
from app.core.time import utc_now
from app.main import app
from app.models.models import PasswordResetChallenge, ProxyApiKey, RefreshToken
from app.services.auth import app_settings as auth_app_settings_module
from app.services.auth import proxy_keys as auth_proxy_keys_module
from app.services.auth_service import (
    consume_password_reset_challenge,
    create_password_reset_challenge,
    create_proxy_api_key,
    enqueue_proxy_api_key_usage,
    get_or_create_app_auth_settings,
    update_auth_settings,
)
from app.services.background_tasks import BackgroundTaskManager
from app.services.profile_invariants import ensure_profile_invariants

TEST_USERNAME = "admin"
TEST_PASSWORD = "11111111"
UPDATED_PASSWORD = "22222222"
TEST_EMAIL = "admin@example.com"


def _clear_auth_settings_snapshot_cache() -> None:
    invalidate = getattr(
        auth_app_settings_module,
        "invalidate_app_auth_settings_snapshot_cache",
        None,
    )
    if callable(invalidate):
        invalidate()


def _clear_proxy_api_key_usage_write_buffer() -> None:
    clear_buffer = getattr(
        auth_proxy_keys_module,
        "clear_proxy_api_key_usage_write_buffer",
        None,
    )
    if callable(clear_buffer):
        clear_buffer()


@pytest.fixture(autouse=True)
def clear_auth_settings_snapshot_cache_fixture() -> Iterator[None]:
    _clear_auth_settings_snapshot_cache()
    _clear_proxy_api_key_usage_write_buffer()
    yield
    _clear_auth_settings_snapshot_cache()
    _clear_proxy_api_key_usage_write_buffer()


async def _reset_auth_state() -> int:
    from app.core.crypto import hash_password

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


async def _build_access_token() -> str:
    async with AsyncSessionLocal() as session:
        settings_row = await get_or_create_app_auth_settings(session)
        return create_access_token(
            subject_id=settings_row.id,
            username=settings_row.username or "",
            token_version=settings_row.token_version,
        )


async def _read_proxy_key_usage(key_id: int) -> tuple[object, object]:
    async with AsyncSessionLocal() as session:
        row = await session.get(ProxyApiKey, key_id)
        assert row is not None
        return row.last_used_at, row.last_used_ip


async def _wait_for_proxy_key_usage(key_id: int) -> tuple[object, object]:
    for _ in range(20):
        last_used_at, last_used_ip = await _read_proxy_key_usage(key_id)
        if last_used_at is not None:
            return last_used_at, last_used_ip
        await asyncio.sleep(0.01)
    return await _read_proxy_key_usage(key_id)


class TestAuthHotPathCache:
    @pytest.mark.asyncio
    async def test_repeated_management_auth_requests_reuse_cached_auth_settings(self):
        await _reset_auth_state()
        transport = ASGITransport(app=app)
        access_token = await _build_access_token()
        auth_settings_queries = 0

        def count_auth_settings_queries(
            conn,
            cursor,
            statement,
            parameters,
            context,
            executemany,
        ) -> None:
            nonlocal auth_settings_queries
            if "app_auth_settings" in statement.lower():
                auth_settings_queries += 1

        engine = get_engine().sync_engine
        event.listen(engine, "before_cursor_execute", count_auth_settings_queries)

        try:
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                client.cookies.set(get_settings().auth_cookie_name, access_token)

                first_response = await client.get("/api/vendors")
                second_response = await client.get("/api/vendors")

            assert first_response.status_code == 200
            assert second_response.status_code == 200
            assert auth_settings_queries == 1
        finally:
            event.remove(engine, "before_cursor_execute", count_auth_settings_queries)
            await _cleanup_auth_state()

    @pytest.mark.asyncio
    async def test_auth_settings_mutation_is_visible_on_next_request(self):
        await _reset_auth_state()
        transport = ASGITransport(app=app)

        try:
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                first_response = await client.get("/api/vendors")
                assert first_response.status_code == 401

            async with AsyncSessionLocal() as session:
                settings_row = await get_or_create_app_auth_settings(session)
                await update_auth_settings(
                    session,
                    settings_row=settings_row,
                    auth_enabled=False,
                    username=settings_row.username,
                    password=None,
                )
                await session.commit()

            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                second_response = await client.get("/api/vendors")

            assert second_response.status_code == 200
        finally:
            await _cleanup_auth_state()

    @pytest.mark.asyncio
    async def test_password_reset_token_version_change_invalidates_cached_auth_subject(
        self,
    ):
        await _reset_auth_state()
        transport = ASGITransport(app=app)
        access_token = await _build_access_token()

        try:
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                client.cookies.set(get_settings().auth_cookie_name, access_token)
                first_response = await client.get("/api/vendors")
                assert first_response.status_code == 200

                async with AsyncSessionLocal() as session:
                    settings_row = await get_or_create_app_auth_settings(session)
                    _, otp_code = await create_password_reset_challenge(
                        session,
                        settings_row=settings_row,
                        requested_ip="127.0.0.1",
                    )
                    await consume_password_reset_challenge(
                        session,
                        otp_code=otp_code,
                        new_password=UPDATED_PASSWORD,
                    )
                    await session.commit()

                stale_response = await client.get("/api/vendors")

            assert stale_response.status_code == 401
            assert stale_response.json() == {"detail": "Authentication required"}
        finally:
            await _cleanup_auth_state()

    @pytest.mark.asyncio
    async def test_proxy_key_usage_metadata_remains_observable_after_successful_auth(
        self,
    ):
        await _reset_auth_state()
        transport = ASGITransport(app=app)

        try:
            async with AsyncSessionLocal() as session:
                settings_row = await get_or_create_app_auth_settings(session)
                raw_key, proxy_key = await create_proxy_api_key(
                    session,
                    name=f"Rank1 Proxy Key {uuid4().hex[:8]}",
                    notes=None,
                    auth_subject_id=settings_row.id,
                )
                proxy_key_id = proxy_key.id
                await session.commit()

            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {raw_key}"},
                    json={"messages": [{"role": "user", "content": "hi"}]},
                )

            assert response.status_code == 400
            assert response.json()["detail"].startswith("Cannot determine model")

            last_used_at, last_used_ip = await _wait_for_proxy_key_usage(proxy_key_id)
            assert last_used_at is not None
            assert last_used_ip == "127.0.0.1"
        finally:
            await _cleanup_auth_state()

    @pytest.mark.asyncio
    async def test_queue_backed_proxy_key_usage_coalesces_latest_update_per_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        await _reset_auth_state()
        manager = BackgroundTaskManager()
        first_commit_started = asyncio.Event()
        release_first_commit = asyncio.Event()
        persisted_snapshots: list[tuple[object, object]] = []
        original_commit = auth_proxy_keys_module._commit_proxy_api_key_usage_snapshot

        async def blocking_commit(snapshot) -> None:
            persisted_snapshots.append((snapshot.last_used_at, snapshot.last_used_ip))
            if len(persisted_snapshots) == 1:
                first_commit_started.set()
                await release_first_commit.wait()
            await original_commit(snapshot)

        try:
            async with AsyncSessionLocal() as session:
                settings_row = await get_or_create_app_auth_settings(session)
                _, proxy_key = await create_proxy_api_key(
                    session,
                    name=f"Rank1 Queue Proxy Key {uuid4().hex[:8]}",
                    notes=None,
                    auth_subject_id=settings_row.id,
                )
                proxy_key_id = proxy_key.id
                await session.commit()

            monkeypatch.setattr(
                auth_proxy_keys_module,
                "_commit_proxy_api_key_usage_snapshot",
                blocking_commit,
            )

            await manager.start()
            first_used_at = utc_now()
            second_used_at = utc_now()
            third_used_at = utc_now()

            assert (
                enqueue_proxy_api_key_usage(
                    manager,
                    key_id=proxy_key_id,
                    last_used_at=first_used_at,
                    last_used_ip="10.0.0.1",
                )
                is True
            )
            await asyncio.wait_for(first_commit_started.wait(), timeout=1)

            assert (
                enqueue_proxy_api_key_usage(
                    manager,
                    key_id=proxy_key_id,
                    last_used_at=second_used_at,
                    last_used_ip="10.0.0.2",
                )
                is True
            )
            assert (
                enqueue_proxy_api_key_usage(
                    manager,
                    key_id=proxy_key_id,
                    last_used_at=third_used_at,
                    last_used_ip="10.0.0.3",
                )
                is True
            )

            release_first_commit.set()
            await manager.wait_for_idle()

            last_used_at, last_used_ip = await _read_proxy_key_usage(proxy_key_id)
            assert last_used_at == third_used_at
            assert last_used_ip == "10.0.0.3"
            assert persisted_snapshots == [
                (first_used_at, "10.0.0.1"),
                (third_used_at, "10.0.0.3"),
            ]
            assert manager.metrics.total_enqueued == 1
            assert manager.metrics.total_completed == 1
        finally:
            release_first_commit.set()
            if manager.started:
                await manager.shutdown()
            await _cleanup_auth_state()
