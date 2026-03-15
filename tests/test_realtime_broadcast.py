from datetime import datetime, timezone
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocket

from app.routers.realtime import SUPPORTED_REALTIME_CHANNELS
from app.services.audit_service import record_audit_log, record_loadbalance_event
from app.services.realtime.connection_manager import ConnectionManager
from app.services.stats.logging import log_request


class MockWebSocket:
    def __init__(self):
        self.accept = AsyncMock()
        self.send_json = AsyncMock()


def make_session_context(session: AsyncMock) -> AsyncMock:
    session_ctx = AsyncMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    return session_ctx


def test_supported_realtime_channels_only_include_dashboard():
    assert SUPPORTED_REALTIME_CHANNELS == {"dashboard"}


@pytest.mark.asyncio
async def test_connection_manager_supports_dashboard_subscriptions():
    manager = ConnectionManager()
    websocket = cast(WebSocket, MockWebSocket())

    connection_id = await manager.connect(websocket)

    assert await manager.subscribe(connection_id, 7, "dashboard") is True

    connection = manager.get_connection(connection_id)

    assert connection is not None
    assert connection.profile_id == 7
    assert connection.channels == {"dashboard"}
    assert manager.rooms[(7, "dashboard")] == {connection_id}

    assert await manager.unsubscribe_channel(connection_id, "dashboard") is True
    assert (7, "dashboard") not in manager.rooms

    await manager.disconnect(connection_id)

    assert manager.get_connection(connection_id) is None
    assert manager.rooms == {}


@pytest.mark.asyncio
async def test_log_request_broadcasts_dashboard_payload():
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    async def fake_refresh(entry):
        entry.id = 321
        entry.created_at = datetime.now(timezone.utc)

    mock_session.refresh = AsyncMock(side_effect=fake_refresh)

    broadcast = AsyncMock()

    with (
        patch(
            "app.core.database.AsyncSessionLocal",
            return_value=make_session_context(mock_session),
        ),
        patch(
            "app.services.stats.logging.connection_manager.broadcast_to_profile",
            broadcast,
        ),
    ):
        request_log_id = await log_request(
            model_id="gpt-4o-mini",
            profile_id=11,
            provider_type="openai",
            endpoint_id=4,
            connection_id=8,
            endpoint_base_url="https://api.openai.com/v1",
            status_code=200,
            response_time_ms=145,
            is_stream=False,
            request_path="/v1/chat/completions",
        )

    assert request_log_id == 321
    assert broadcast.await_count == 1

    dashboard_call = broadcast.await_args_list[0].kwargs

    assert dashboard_call["profile_id"] == 11
    assert dashboard_call["channel"] == "dashboard"
    assert dashboard_call["message"]["type"] == "dashboard.update"
    assert dashboard_call["message"]["request_log"]["id"] == 321


@pytest.mark.asyncio
async def test_record_audit_log_commits_without_broadcasts():
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    with patch(
        "app.core.database.AsyncSessionLocal",
        return_value=make_session_context(mock_session),
    ):
        await record_audit_log(
            request_log_id=55,
            profile_id=3,
            provider_id=1,
            model_id="gpt-4o-mini",
            request_method="POST",
            request_url="https://api.openai.com/v1/chat/completions",
            request_headers={"authorization": "Bearer sk-test"},
            request_body=b'{"model":"gpt-4o-mini"}',
            response_status=200,
            response_headers={"content-type": "application/json"},
            response_body=b'{"id":"resp_123"}',
            is_stream=False,
            duration_ms=120,
            capture_bodies=True,
        )

    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_loadbalance_event_commits_without_broadcasts():
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    with patch(
        "app.core.database.AsyncSessionLocal",
        return_value=make_session_context(mock_session),
    ):
        await record_loadbalance_event(
            profile_id=6,
            connection_id=13,
            event_type="opened",
            failure_kind="timeout",
            consecutive_failures=4,
            cooldown_seconds=30.0,
            blocked_until_mono=99.5,
            model_id="gpt-4o-mini",
            endpoint_id=12,
            provider_id=1,
            failure_threshold=3,
            backoff_multiplier=2.0,
            max_cooldown_seconds=120,
        )

    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_log_request_falls_back_to_dashboard_dirty_signal_when_serialization_fails():
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    async def fake_refresh(entry):
        entry.id = 654
        entry.created_at = datetime.now(timezone.utc)

    mock_session.refresh = AsyncMock(side_effect=fake_refresh)

    broadcast = AsyncMock()

    with (
        patch(
            "app.core.database.AsyncSessionLocal",
            return_value=make_session_context(mock_session),
        ),
        patch(
            "app.services.stats.logging.RequestLogResponse.model_validate",
            side_effect=ValueError("boom"),
        ),
        patch(
            "app.services.stats.logging.connection_manager.broadcast_to_profile",
            broadcast,
        ),
    ):
        await log_request(
            model_id="gpt-4o-mini",
            profile_id=11,
            provider_type="openai",
            endpoint_id=4,
            connection_id=8,
            endpoint_base_url="https://api.openai.com/v1",
            status_code=500,
            response_time_ms=145,
            is_stream=False,
            request_path="/v1/chat/completions",
        )

    assert [call.kwargs["message"]["type"] for call in broadcast.await_args_list] == [
        "dashboard.dirty",
    ]
