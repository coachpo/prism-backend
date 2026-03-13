from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


@pytest.mark.asyncio
async def test_connection_manager_supports_multi_channel_subscriptions():
    manager = ConnectionManager()
    websocket = MockWebSocket()

    connection_id = await manager.connect(websocket)

    assert await manager.subscribe(connection_id, 7, "dashboard") is True
    assert await manager.subscribe(connection_id, 7, "request_logs") is True

    connection = manager.get_connection(connection_id)

    assert connection is not None
    assert connection.profile_id == 7
    assert connection.channels == {"dashboard", "request_logs"}
    assert manager.rooms[(7, "dashboard")] == {connection_id}
    assert manager.rooms[(7, "request_logs")] == {connection_id}

    assert await manager.unsubscribe_channel(connection_id, "dashboard") is True
    assert (7, "dashboard") not in manager.rooms
    assert manager.rooms[(7, "request_logs")] == {connection_id}

    await manager.disconnect(connection_id)

    assert manager.get_connection(connection_id) is None
    assert (7, "request_logs") not in manager.rooms


@pytest.mark.asyncio
async def test_log_request_broadcasts_request_log_payload():
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
    assert broadcast.await_count == 3

    dashboard_call = broadcast.await_args_list[0].kwargs
    request_logs_call = broadcast.await_args_list[1].kwargs
    statistics_call = broadcast.await_args_list[2].kwargs

    assert dashboard_call["profile_id"] == 11
    assert dashboard_call["channel"] == "dashboard"
    assert dashboard_call["message"]["type"] == "dashboard.update"
    assert dashboard_call["message"]["request_log"]["id"] == 321

    assert request_logs_call["channel"] == "request_logs"
    assert request_logs_call["message"]["type"] == "request_logs.new"
    assert (
        request_logs_call["message"]["request_log"]["request_path"]
        == "/v1/chat/completions"
    )

    assert statistics_call["channel"] == "statistics"
    assert statistics_call["message"]["type"] == "statistics.new"


@pytest.mark.asyncio
async def test_record_audit_log_broadcasts_audit_ready():
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    async def fake_refresh(entry):
        entry.id = 88

    mock_session.refresh = AsyncMock(side_effect=fake_refresh)

    broadcast = AsyncMock()

    with (
        patch(
            "app.core.database.AsyncSessionLocal",
            return_value=make_session_context(mock_session),
        ),
        patch(
            "app.services.audit_service.connection_manager.broadcast_to_profile",
            broadcast,
        ),
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

    broadcast.assert_awaited_once()
    kwargs = broadcast.await_args.kwargs
    assert kwargs["profile_id"] == 3
    assert kwargs["channel"] == "request_logs"
    assert kwargs["message"] == {
        "type": "request_logs.audit_ready",
        "request_log_id": 55,
        "audit_log_id": 88,
    }


@pytest.mark.asyncio
async def test_record_loadbalance_event_broadcasts_event_payload():
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    async def fake_refresh(entry):
        entry.id = 901
        entry.created_at = datetime.now(timezone.utc)

    mock_session.refresh = AsyncMock(side_effect=fake_refresh)

    broadcast = AsyncMock()

    with (
        patch(
            "app.core.database.AsyncSessionLocal",
            return_value=make_session_context(mock_session),
        ),
        patch(
            "app.services.audit_service.connection_manager.broadcast_to_profile",
            broadcast,
        ),
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

    broadcast.assert_awaited_once()
    kwargs = broadcast.await_args.kwargs
    assert kwargs["profile_id"] == 6
    assert kwargs["channel"] == "loadbalance_events"
    assert kwargs["message"]["type"] == "loadbalance_events.new"
    assert kwargs["message"]["event"]["id"] == 901
    assert kwargs["message"]["event"]["event_type"] == "opened"


@pytest.mark.asyncio
async def test_log_request_falls_back_to_dirty_signals_when_serialization_fails():
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
        "request_logs.dirty",
        "statistics.dirty",
    ]
