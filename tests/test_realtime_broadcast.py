import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocket

from app.models.models import ModelConfig
from app.routers.proxy_domains.attempt_logging import _record_attempt_audit
from app.routers.realtime import SUPPORTED_REALTIME_CHANNELS
from app.services.loadbalancer_support.attempts import build_attempt_plan
from app.services.loadbalancer_support.events import record_failed_transition
from app.services.loadbalancer_support.recovery import (
    mark_connection_failed,
    mark_connection_recovered,
)
from app.services.loadbalancer_support.state import _recovery_state
from app.services.audit_service import (
    AUDIT_LOG_MAX_RETRIES,
    AUDIT_LOG_RETRY_DELAY_SECONDS,
    record_audit_log,
    record_loadbalance_event,
)
from app.services.background_tasks import BackgroundTaskManager
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
async def test_log_request_enqueues_dashboard_broadcast_payload() -> None:
    log_session = AsyncMock()
    log_session.add = MagicMock()
    log_session.commit = AsyncMock()
    broadcast_session = AsyncMock()
    captured_entry = {}

    async def fake_refresh(entry):
        entry.id = 321
        entry.created_at = datetime.now(timezone.utc)
        captured_entry["entry"] = entry

    log_session.refresh = AsyncMock(side_effect=fake_refresh)

    broadcast = AsyncMock()
    build_dashboard_update_message = AsyncMock()
    enqueued_job = {}
    dashboard_message = {
        "type": "dashboard.update",
        "request_log": {"id": 321},
        "stats_summary_24h": {"total_requests": 1},
        "provider_summary_24h": {"total_requests": 1},
        "spending_summary_30d": {"summary": {"total_cost_micros": 0}},
        "throughput_24h": {"total_requests": 1},
        "routing_route_24h": None,
    }
    build_dashboard_update_message.return_value = dashboard_message

    def capture_enqueue(*, name, run, max_retries=0, retry_delay_seconds=0.0):
        enqueued_job.update(
            name=name,
            run=run,
            max_retries=max_retries,
            retry_delay_seconds=retry_delay_seconds,
        )

    with (
        patch(
            "app.core.database.AsyncSessionLocal",
            side_effect=[
                make_session_context(log_session),
                make_session_context(broadcast_session),
            ],
        ),
        patch(
            "app.services.stats.logging.build_dashboard_update_message",
            build_dashboard_update_message,
        ),
        patch(
            "app.services.stats.logging.connection_manager.broadcast_to_profile",
            broadcast,
        ),
        patch(
            "app.services.stats.logging.background_task_manager.enqueue",
            MagicMock(side_effect=capture_enqueue),
        ) as enqueue,
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
        log_session.commit.assert_awaited_once()
        enqueue.assert_called_once()
        assert enqueued_job["name"] == "dashboard-update:11:321"
        assert build_dashboard_update_message.await_count == 0
        assert broadcast.await_count == 0

        broadcast_session.get = AsyncMock(return_value=captured_entry["entry"])
        await enqueued_job["run"]()

    broadcast_session.get.assert_awaited_once()
    assert build_dashboard_update_message.await_count == 1
    assert broadcast.await_count == 1

    dashboard_call = broadcast.await_args_list[0].kwargs

    assert dashboard_call["profile_id"] == 11
    assert dashboard_call["channel"] == "dashboard"
    assert dashboard_call["message"]["type"] == "dashboard.update"
    assert dashboard_call["message"]["request_log"]["id"] == 321
    assert dashboard_call["message"]["stats_summary_24h"]["total_requests"] == 1


@pytest.mark.asyncio
async def test_record_audit_log_enqueues_persistence_with_request_log_linkage() -> None:
    persist_session = AsyncMock()
    persist_session.add = MagicMock()
    persist_session.commit = AsyncMock()
    enqueued_job = {}

    def capture_enqueue(*, name, run, max_retries=0, retry_delay_seconds=0.0):
        enqueued_job.update(
            name=name,
            run=run,
            max_retries=max_retries,
            retry_delay_seconds=retry_delay_seconds,
        )

    with (
        patch(
            "app.core.database.AsyncSessionLocal",
            return_value=make_session_context(persist_session),
        ),
        patch(
            "app.services.audit_service.background_task_manager.enqueue",
            MagicMock(side_effect=capture_enqueue),
        ) as enqueue,
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

        enqueue.assert_called_once()
        assert enqueued_job["name"] == "audit-log:3:55"
        assert enqueued_job["max_retries"] == AUDIT_LOG_MAX_RETRIES
        assert enqueued_job["retry_delay_seconds"] == AUDIT_LOG_RETRY_DELAY_SECONDS
        assert persist_session.commit.await_count == 0

        await enqueued_job["run"]()

    persist_session.commit.assert_awaited_once()
    persist_session.add.assert_called_once()
    audit_entry = persist_session.add.call_args[0][0]
    assert audit_entry.request_log_id == 55
    assert audit_entry.profile_id == 3
    assert audit_entry.request_body == '{"model":"gpt-4o-mini"}'
    assert audit_entry.response_body == '{"id":"resp_123"}'
    assert (
        json.loads(audit_entry.request_headers)["authorization"] == "Bearer [REDACTED]"
    )


@pytest.mark.asyncio
async def test_record_audit_log_returns_when_enqueue_fails() -> None:
    session_factory = MagicMock()

    with (
        patch("app.core.database.AsyncSessionLocal", session_factory),
        patch(
            "app.services.audit_service.background_task_manager.enqueue",
            MagicMock(side_effect=RuntimeError("queue unavailable")),
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

    session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_record_audit_log_background_failure_stays_off_path() -> None:
    manager = BackgroundTaskManager()
    await manager.start()

    persist_session = AsyncMock()
    persist_session.add = MagicMock()
    persist_session.commit = AsyncMock(side_effect=RuntimeError("db write failed"))

    with (
        patch(
            "app.core.database.AsyncSessionLocal",
            return_value=make_session_context(persist_session),
        ),
        patch("app.services.audit_service.background_task_manager", manager),
    ):
        await record_audit_log(
            request_log_id=77,
            profile_id=4,
            provider_id=1,
            model_id="gpt-4o-mini",
            request_method="POST",
            request_url="https://api.openai.com/v1/chat/completions",
            request_headers={"authorization": "Bearer sk-test"},
            request_body=b'{"model":"gpt-4o-mini"}',
            response_status=500,
            response_headers={"content-type": "application/json"},
            response_body=b'{"error":"boom"}',
            is_stream=False,
            duration_ms=120,
            capture_bodies=True,
        )

        await manager.wait_for_idle()

    await manager.shutdown()

    assert persist_session.add.call_count == AUDIT_LOG_MAX_RETRIES + 1
    assert persist_session.commit.await_count == AUDIT_LOG_MAX_RETRIES + 1
    first_audit_entry = persist_session.add.call_args_list[0].args[0]
    assert first_audit_entry.request_log_id == 77


@pytest.mark.asyncio
async def test_record_audit_log_continues_after_terminal_failure() -> None:
    manager = BackgroundTaskManager()
    await manager.start()

    failing_sessions = []
    for _ in range(AUDIT_LOG_MAX_RETRIES + 1):
        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock(side_effect=RuntimeError("db write failed"))
        failing_sessions.append(session)

    success_session = AsyncMock()
    success_session.add = MagicMock()
    success_session.commit = AsyncMock()

    session_contexts = [
        make_session_context(session)
        for session in [*failing_sessions, success_session]
    ]

    with (
        patch(
            "app.core.database.AsyncSessionLocal",
            side_effect=session_contexts,
        ),
        patch("app.services.audit_service.background_task_manager", manager),
    ):
        await record_audit_log(
            request_log_id=77,
            profile_id=4,
            provider_id=1,
            model_id="gpt-4o-mini",
            request_method="POST",
            request_url="https://api.openai.com/v1/chat/completions",
            request_headers={"authorization": "Bearer sk-test"},
            request_body=b'{"model":"gpt-4o-mini"}',
            response_status=500,
            response_headers={"content-type": "application/json"},
            response_body=b'{"error":"boom"}',
            is_stream=False,
            duration_ms=120,
            capture_bodies=True,
        )
        await record_audit_log(
            request_log_id=78,
            profile_id=4,
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

        await manager.wait_for_idle()
        metrics = manager.metrics

    await manager.shutdown()

    assert metrics.total_enqueued == 2
    assert metrics.total_completed == 1
    assert metrics.retry_attempts_total == AUDIT_LOG_MAX_RETRIES
    assert metrics.terminal_failures_total == 1
    assert metrics.last_failure is not None
    assert metrics.last_failure.job_kind == "audit-log"
    assert metrics.last_failure.phase == "run"
    assert metrics.last_failure.failure_kind == "job_exception"
    assert success_session.commit.await_count == 1
    success_entry = success_session.add.call_args[0][0]
    assert success_entry.request_log_id == 78


@pytest.mark.asyncio
async def test_record_attempt_audit_skips_when_request_log_id_missing() -> None:
    deps = MagicMock()
    deps.record_audit_log_fn = AsyncMock()

    state = MagicMock()
    state.profile_id = 3
    state.setup.audit_enabled = True
    state.setup.provider_id = 1
    state.setup.model_id = "gpt-4o-mini"
    state.setup.method = "POST"
    state.setup.audit_capture_bodies = True

    target = MagicMock()
    target.connection.endpoint_id = 4
    target.connection.id = 8
    target.connection.endpoint_rel.base_url = "https://api.openai.com/v1"
    target.description = "Primary endpoint"
    target.upstream_url = "https://api.openai.com/v1/chat/completions"
    target.headers = {"authorization": "Bearer sk-test"}
    target.endpoint_body = b'{"model":"gpt-4o-mini"}'

    await _record_attempt_audit(
        deps=deps,
        request_log_id=None,
        state=state,
        target=target,
        status_code=200,
        response_headers={"content-type": "application/json"},
        response_body=b'{"id":"resp_123"}',
        is_stream=False,
        elapsed_ms=120,
    )

    deps.record_audit_log_fn.assert_not_awaited()


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


def test_record_failed_transition_enqueues_managed_loadbalance_event() -> None:
    persist_session = AsyncMock()
    persist_session.add = MagicMock()
    persist_session.commit = AsyncMock()
    enqueued_job = {}

    def capture_enqueue(*, name, run, max_retries=0, retry_delay_seconds=0.0):
        enqueued_job.update(
            name=name,
            run=run,
            max_retries=max_retries,
            retry_delay_seconds=retry_delay_seconds,
        )

    with (
        patch(
            "app.core.database.AsyncSessionLocal",
            return_value=make_session_context(persist_session),
        ),
        patch(
            "app.services.loadbalancer_support.events.background_task_manager.enqueue",
            MagicMock(side_effect=capture_enqueue),
        ) as enqueue,
    ):
        record_failed_transition(
            event_type="opened",
            profile_id=6,
            connection_id=13,
            failure_kind="timeout",
            consecutive_failures=4,
            cooldown_seconds=30.0,
            blocked_until_mono=99.5,
            model_id="gpt-4o-mini",
            endpoint_id=12,
            provider_id=1,
        )

        enqueue.assert_called_once()
        assert enqueued_job["name"] == "loadbalance-event:6:13:opened"
        assert enqueued_job["max_retries"] == 0
        assert enqueued_job["retry_delay_seconds"] == 0.0
        assert persist_session.commit.await_count == 0

        import asyncio

        asyncio.run(enqueued_job["run"]())

    persist_session.commit.assert_awaited_once()
    persist_session.add.assert_called_once()
    event_entry = persist_session.add.call_args[0][0]
    assert event_entry.profile_id == 6
    assert event_entry.connection_id == 13
    assert event_entry.event_type == "opened"


@pytest.mark.asyncio
async def test_loadbalance_transition_background_failure_stays_off_path() -> None:
    manager = BackgroundTaskManager()
    await manager.start()

    persist_session = AsyncMock()
    persist_session.add = MagicMock()
    persist_session.commit = AsyncMock(side_effect=RuntimeError("db write failed"))

    with (
        patch(
            "app.core.database.AsyncSessionLocal",
            return_value=make_session_context(persist_session),
        ),
        patch(
            "app.services.loadbalancer_support.events.background_task_manager", manager
        ),
    ):
        record_failed_transition(
            event_type="opened",
            profile_id=6,
            connection_id=13,
            failure_kind="timeout",
            consecutive_failures=4,
            cooldown_seconds=30.0,
            blocked_until_mono=99.5,
            model_id="gpt-4o-mini",
            endpoint_id=12,
            provider_id=1,
        )

        await manager.wait_for_idle()

    await manager.shutdown()

    persist_session.add.assert_called_once()
    persist_session.commit.assert_awaited_once()


def test_mark_connection_failed_preserves_state_when_loadbalance_enqueue_fails() -> (
    None
):
    _recovery_state.clear()

    with patch(
        "app.services.loadbalancer_support.events.background_task_manager.enqueue",
        MagicMock(side_effect=RuntimeError("queue unavailable")),
    ):
        mark_connection_failed(
            profile_id=4,
            connection_id=9,
            base_cooldown_seconds=30.0,
            now_mono=100.0,
            failure_kind="timeout",
            model_id="gpt-4o-mini",
            endpoint_id=12,
            provider_id=1,
        )

    state = _recovery_state[(4, 9)]
    assert state["consecutive_failures"] == 1
    _recovery_state.clear()


def test_mark_connection_recovered_clears_state_when_loadbalance_enqueue_fails() -> (
    None
):
    _recovery_state.clear()
    _recovery_state[(4, 9)] = {
        "consecutive_failures": 3,
        "blocked_until_mono": 140.0,
        "last_cooldown_seconds": 30.0,
        "last_failure_kind": "timeout",
        "probe_eligible_logged": False,
    }

    with patch(
        "app.services.loadbalancer_support.events.background_task_manager.enqueue",
        MagicMock(side_effect=RuntimeError("queue unavailable")),
    ):
        mark_connection_recovered(
            profile_id=4,
            connection_id=9,
            model_id="gpt-4o-mini",
            endpoint_id=12,
            provider_id=1,
        )

    assert (4, 9) not in _recovery_state


def test_build_attempt_plan_keeps_probe_eligible_connection_when_loadbalance_enqueue_fails() -> (
    None
):
    _recovery_state.clear()
    connection = SimpleNamespace(
        id=7,
        priority=0,
        health_status="healthy",
        is_active=True,
        endpoint_rel=object(),
        endpoint_id=12,
    )
    model_config = cast(
        ModelConfig,
        SimpleNamespace(
            connections=[connection],
            lb_strategy="failover",
            failover_recovery_enabled=True,
            model_id="gpt-4o-mini",
            provider_id=1,
        ),
    )
    _recovery_state[(5, 7)] = {
        "consecutive_failures": 2,
        "blocked_until_mono": 10.0,
        "last_cooldown_seconds": 30.0,
        "last_failure_kind": "timeout",
        "probe_eligible_logged": False,
    }

    with patch(
        "app.services.loadbalancer_support.events.background_task_manager.enqueue",
        MagicMock(side_effect=RuntimeError("queue unavailable")),
    ):
        attempt_plan = build_attempt_plan(
            profile_id=5,
            model_config=model_config,
            now_mono=100.0,
        )

    assert attempt_plan == [connection]
    assert _recovery_state[(5, 7)]["probe_eligible_logged"] is True
    _recovery_state.clear()


@pytest.mark.asyncio
async def test_log_request_returns_id_when_dashboard_enqueue_fails() -> None:
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    async def fake_refresh(entry):
        entry.id = 654
        entry.created_at = datetime.now(timezone.utc)

    mock_session.refresh = AsyncMock(side_effect=fake_refresh)

    build_dashboard_update_message = AsyncMock()
    broadcast = AsyncMock()

    with (
        patch(
            "app.core.database.AsyncSessionLocal",
            return_value=make_session_context(mock_session),
        ),
        patch(
            "app.services.stats.logging.background_task_manager.enqueue",
            MagicMock(side_effect=RuntimeError("queue unavailable")),
        ),
        patch(
            "app.services.stats.logging.connection_manager.broadcast_to_profile",
            broadcast,
        ),
        patch(
            "app.services.stats.logging.build_dashboard_update_message",
            build_dashboard_update_message,
        ),
    ):
        request_log_id = await log_request(
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

    assert request_log_id == 654
    mock_session.commit.assert_awaited_once()
    assert build_dashboard_update_message.await_count == 0
    assert broadcast.await_count == 0


@pytest.mark.asyncio
async def test_log_request_keeps_committed_id_when_background_build_fails() -> None:
    manager = BackgroundTaskManager()
    await manager.start()

    log_session = AsyncMock()
    log_session.add = MagicMock()
    log_session.commit = AsyncMock()
    broadcast_session = AsyncMock()
    captured_entry = {}

    async def fake_refresh(entry):
        entry.id = 987
        entry.created_at = datetime.now(timezone.utc)
        captured_entry["entry"] = entry

    log_session.refresh = AsyncMock(side_effect=fake_refresh)
    broadcast_session.get = AsyncMock(
        side_effect=lambda model, request_log_id: captured_entry["entry"]
    )
    build_dashboard_update_message = AsyncMock(side_effect=ValueError("boom"))
    broadcast = AsyncMock()

    with (
        patch(
            "app.core.database.AsyncSessionLocal",
            side_effect=[
                make_session_context(log_session),
                make_session_context(broadcast_session),
            ],
        ),
        patch("app.services.stats.logging.background_task_manager", manager),
        patch(
            "app.services.stats.logging.build_dashboard_update_message",
            build_dashboard_update_message,
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
            status_code=500,
            response_time_ms=145,
            is_stream=False,
            request_path="/v1/chat/completions",
        )

        assert request_log_id == 987
        log_session.commit.assert_awaited_once()
        await manager.wait_for_idle()

    await manager.shutdown()

    build_dashboard_update_message.assert_awaited_once()
    assert broadcast.await_count == 0
