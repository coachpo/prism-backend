import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from fastapi import WebSocket

import app.services.stats.logging as stats_logging
from app.models.models import ModelConfig
from app.routers.proxy_domains.attempt_outcome_reporting import record_attempt_audit
from app.routers.realtime import SUPPORTED_REALTIME_CHANNELS, websocket_endpoint
from app.services.loadbalancer.events import record_failed_transition
from app.services.loadbalancer.planner import build_attempt_plan
from app.services.loadbalancer.recovery import (
    record_connection_failure,
    record_connection_recovery,
)
from app.services.audit_service import (
    AUDIT_LOG_MAX_RETRIES,
    AUDIT_LOG_RETRY_DELAY_SECONDS,
    record_audit_log,
    record_loadbalance_event,
)
from app.services.background_tasks import BackgroundTaskManager
from app.services.realtime.connection_manager import ConnectionManager
from app.services.stats.logging import (
    broadcast_dashboard_update_for_request_log,
    enqueue_dashboard_update_broadcast,
    log_request,
)


class MockWebSocket:
    def __init__(self):
        self.accept = AsyncMock()
        self.send_json = AsyncMock()


def make_session_context(session: AsyncMock) -> AsyncMock:
    session_ctx = AsyncMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    return session_ctx


def make_failover_policy(**overrides):
    from app.services.loadbalancer.policy import EffectiveLoadbalancePolicy

    return EffectiveLoadbalancePolicy(
        strategy_type=cast(
            Literal["single", "failover"],
            overrides.get("strategy_type", "failover"),
        ),
        failover_recovery_enabled=cast(
            bool, overrides.get("failover_recovery_enabled", True)
        ),
        failover_cooldown_seconds=float(
            cast(float | int, overrides.get("failover_cooldown_seconds", 30.0))
        ),
        failover_failure_threshold=cast(
            int, overrides.get("failover_failure_threshold", 2)
        ),
        failover_backoff_multiplier=float(
            cast(float | int, overrides.get("failover_backoff_multiplier", 2.0))
        ),
        failover_max_cooldown_seconds=cast(
            int, overrides.get("failover_max_cooldown_seconds", 900)
        ),
        failover_jitter_ratio=float(
            cast(float | int, overrides.get("failover_jitter_ratio", 0.2))
        ),
        failover_auth_error_cooldown_seconds=cast(
            int, overrides.get("failover_auth_error_cooldown_seconds", 1800)
        ),
        failover_ban_mode=cast(
            Literal["off", "temporary", "manual"],
            overrides.get("failover_ban_mode", "off"),
        ),
        failover_max_cooldown_strikes_before_ban=cast(
            int, overrides.get("failover_max_cooldown_strikes_before_ban", 0)
        ),
        failover_ban_duration_seconds=cast(
            int, overrides.get("failover_ban_duration_seconds", 0)
        ),
    )


def as_websocket(mock_websocket: MockWebSocket) -> WebSocket:
    return cast(WebSocket, cast(object, mock_websocket))


def test_supported_realtime_channels_only_include_dashboard():
    assert SUPPORTED_REALTIME_CHANNELS == {"dashboard"}


@pytest.mark.asyncio
async def test_connection_manager_supports_dashboard_subscriptions():
    manager = ConnectionManager()
    websocket = as_websocket(MockWebSocket())

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
async def test_disconnect_cleans_up_active_subscription_membership():
    manager = ConnectionManager()
    websocket = as_websocket(MockWebSocket())

    connection_id = await manager.connect(websocket)
    assert await manager.subscribe(connection_id, 7, "dashboard") is True

    await manager.disconnect(connection_id)

    assert manager.get_connection(connection_id) is None
    assert (7, "dashboard") not in manager.rooms
    assert manager.rooms == {}


@pytest.mark.asyncio
async def test_broadcast_to_profile_removes_connection_when_send_fails():
    manager = ConnectionManager()
    websocket = as_websocket(MockWebSocket())
    send_json = cast(AsyncMock, websocket.send_json)
    send_json.side_effect = RuntimeError("socket closed")

    connection_id = await manager.connect(websocket)
    assert await manager.subscribe(connection_id, 7, "dashboard") is True

    delivered = await manager.broadcast_to_profile(
        7,
        "dashboard",
        {"type": "dashboard.update"},
    )

    assert delivered == 1
    assert manager.get_connection(connection_id) is None
    assert (7, "dashboard") not in manager.rooms
    assert send_json.await_count == 1


@pytest.mark.asyncio
async def test_websocket_endpoint_returns_after_failed_initial_control_send():
    websocket = as_websocket(MockWebSocket())
    websocket.receive_json = AsyncMock(
        side_effect=AssertionError("route should stop after failed send")
    )

    connection = MagicMock()
    connection.authenticated = False
    connection.send_json = AsyncMock(return_value=False)
    db = AsyncMock()
    settings_row = SimpleNamespace(
        auth_enabled=False,
        username="operator",
        id=1,
        token_version=1,
    )

    with (
        patch(
            "app.routers.realtime.connection_manager.connect",
            AsyncMock(return_value="c1"),
        ),
        patch(
            "app.routers.realtime.connection_manager.get_connection",
            MagicMock(return_value=connection),
        ),
        patch(
            "app.routers.realtime.connection_manager.disconnect", AsyncMock()
        ) as disconnect_mock,
        patch(
            "app.routers.realtime.get_settings",
            MagicMock(return_value=SimpleNamespace(auth_cookie_name="auth_cookie")),
        ),
        patch(
            "app.routers.realtime.get_or_create_app_auth_settings",
            AsyncMock(return_value=settings_row),
        ),
        patch(
            "app.routers.realtime.authenticate_websocket", AsyncMock(return_value=None)
        ),
    ):
        await websocket_endpoint(websocket=websocket, db=db)

    connection.send_json.assert_awaited_once_with(
        {"type": "authenticated", "username": "operator"}
    )
    disconnect_mock.assert_awaited_once_with("c1")
    websocket.receive_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_broadcast_to_profile_does_not_retry_stale_connection_after_failed_send():
    manager = ConnectionManager()
    websocket = as_websocket(MockWebSocket())
    send_json = cast(AsyncMock, websocket.send_json)
    send_json.side_effect = RuntimeError("socket closed")

    connection_id = await manager.connect(websocket)
    assert await manager.subscribe(connection_id, 7, "dashboard") is True

    first_delivered = await manager.broadcast_to_profile(
        7,
        "dashboard",
        {"type": "dashboard.update", "seq": 1},
    )
    second_delivered = await manager.broadcast_to_profile(
        7,
        "dashboard",
        {"type": "dashboard.update", "seq": 2},
    )

    assert first_delivered == 1
    assert second_delivered == 0
    assert manager.get_connection(connection_id) is None
    assert (7, "dashboard") not in manager.rooms
    assert send_json.await_count == 1


@pytest.mark.asyncio
async def test_send_to_connection_returns_false_and_cleans_up_when_send_fails():
    manager = ConnectionManager()
    websocket = as_websocket(MockWebSocket())
    send_json = cast(AsyncMock, websocket.send_json)
    send_json.side_effect = RuntimeError("socket closed")

    connection_id = await manager.connect(websocket)
    assert await manager.subscribe(connection_id, 7, "dashboard") is True

    sent = await manager.send_to_connection(
        connection_id,
        {"type": "heartbeat"},
    )

    assert sent is False
    assert manager.get_connection(connection_id) is None
    assert (7, "dashboard") not in manager.rooms
    assert send_json.await_count == 1


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
            "app.services.stats.logging.connection_manager.has_subscribers",
            MagicMock(return_value=True),
            create=True,
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
            endpoint_base_url="https://api.openai.com",
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
async def test_broadcast_dashboard_update_skips_work_without_dashboard_subscribers() -> (
    None
):
    session_factory = MagicMock()
    build_dashboard_update_message = AsyncMock()
    broadcast = AsyncMock()

    with (
        patch(
            "app.services.stats.logging.connection_manager.has_subscribers",
            MagicMock(return_value=False),
            create=True,
        ) as has_subscribers,
        patch("app.core.database.AsyncSessionLocal", session_factory),
        patch(
            "app.services.stats.logging.build_dashboard_update_message",
            build_dashboard_update_message,
        ),
        patch(
            "app.services.stats.logging.connection_manager.broadcast_to_profile",
            broadcast,
        ),
    ):
        await broadcast_dashboard_update_for_request_log(
            request_log_id=321,
            profile_id=11,
        )

    has_subscribers.assert_called_once_with(profile_id=11, channel="dashboard")
    session_factory.assert_not_called()
    build_dashboard_update_message.assert_not_awaited()
    broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_dashboard_update_broadcast_coalesces_same_profile_jobs() -> None:
    enqueued_jobs = []
    broadcast = AsyncMock()
    monkeypatch_latest = {}
    monkeypatch_pending = set()

    def capture_enqueue(*, name, run, max_retries=0, retry_delay_seconds=0.0):
        enqueued_jobs.append(
            {
                "name": name,
                "run": run,
                "max_retries": max_retries,
                "retry_delay_seconds": retry_delay_seconds,
            }
        )

    with (
        patch.object(
            stats_logging,
            "_dashboard_update_latest_request_log_ids",
            monkeypatch_latest,
            create=True,
        ),
        patch.object(
            stats_logging,
            "_dashboard_update_enqueued_profiles",
            monkeypatch_pending,
            create=True,
        ),
        patch(
            "app.services.stats.logging.background_task_manager.enqueue",
            MagicMock(side_effect=capture_enqueue),
        ) as enqueue,
        patch(
            "app.services.stats.logging.connection_manager.has_subscribers",
            MagicMock(return_value=True),
            create=True,
        ),
        patch(
            "app.services.stats.logging.broadcast_dashboard_update_for_request_log",
            broadcast,
        ),
        patch(
            "app.services.stats.logging.get_settings",
            MagicMock(
                return_value=SimpleNamespace(dashboard_update_debounce_seconds=0.0)
            ),
            create=True,
        ),
    ):
        enqueue_dashboard_update_broadcast(request_log_id=101, profile_id=11)
        enqueue_dashboard_update_broadcast(request_log_id=102, profile_id=11)
        enqueue_dashboard_update_broadcast(request_log_id=201, profile_id=22)

        assert enqueue.call_count == 2
        assert [job["name"] for job in enqueued_jobs] == [
            "dashboard-update:11:101",
            "dashboard-update:22:201",
        ]

        await enqueued_jobs[0]["run"]()
        await enqueued_jobs[1]["run"]()

    assert broadcast.await_args_list == [
        call(request_log_id=102, profile_id=11),
        call(request_log_id=201, profile_id=22),
    ]


@pytest.mark.asyncio
async def test_enqueue_dashboard_update_broadcast_requeues_newer_same_profile_update_after_earlier_failure() -> (
    None
):
    enqueued_jobs = []
    monkeypatch_latest = {}
    monkeypatch_pending = set()

    def capture_enqueue(*, name, run, max_retries=0, retry_delay_seconds=0.0):
        enqueued_jobs.append(
            {
                "name": name,
                "run": run,
                "max_retries": max_retries,
                "retry_delay_seconds": retry_delay_seconds,
            }
        )

    async def fail_then_leave_newer_pending(*, request_log_id: int, profile_id: int):
        if request_log_id == 101:
            enqueue_dashboard_update_broadcast(
                request_log_id=102, profile_id=profile_id
            )
            raise ValueError("boom")

        return None

    broadcast = AsyncMock(side_effect=fail_then_leave_newer_pending)

    with (
        patch.object(
            stats_logging,
            "_dashboard_update_latest_request_log_ids",
            monkeypatch_latest,
            create=True,
        ),
        patch.object(
            stats_logging,
            "_dashboard_update_enqueued_profiles",
            monkeypatch_pending,
            create=True,
        ),
        patch(
            "app.services.stats.logging.background_task_manager.enqueue",
            MagicMock(side_effect=capture_enqueue),
        ) as enqueue,
        patch(
            "app.services.stats.logging.connection_manager.has_subscribers",
            MagicMock(return_value=True),
            create=True,
        ),
        patch(
            "app.services.stats.logging.broadcast_dashboard_update_for_request_log",
            broadcast,
        ),
        patch(
            "app.services.stats.logging.get_settings",
            MagicMock(
                return_value=SimpleNamespace(dashboard_update_debounce_seconds=0.0)
            ),
            create=True,
        ),
    ):
        enqueue_dashboard_update_broadcast(request_log_id=101, profile_id=11)

        assert enqueue.call_count == 1
        assert enqueued_jobs[0]["name"] == "dashboard-update:11:101"

        with pytest.raises(ValueError, match="boom"):
            await enqueued_jobs[0]["run"]()

        assert enqueue.call_count == 2
        assert enqueued_jobs[1]["name"] == "dashboard-update:11:102"
        assert monkeypatch_pending == {11}
        assert monkeypatch_latest == {11: 102}

        await enqueued_jobs[1]["run"]()

    assert broadcast.await_args_list == [
        call(request_log_id=101, profile_id=11),
        call(request_log_id=102, profile_id=11),
    ]
    assert monkeypatch_pending == set()
    assert monkeypatch_latest == {}


@pytest.mark.asyncio
async def test_enqueue_dashboard_update_broadcast_skips_debounce_worker_without_subscribers() -> (
    None
):
    sleep_mock = AsyncMock(
        side_effect=AssertionError("dashboard debounce should not run")
    )
    manager = BackgroundTaskManager(worker_count=1, sleep_fn=sleep_mock)
    await manager.start()
    unrelated_completed = asyncio.Event()
    broadcast = AsyncMock()

    async def run_unrelated() -> None:
        unrelated_completed.set()

    with (
        patch("app.services.stats.logging.background_task_manager", manager),
        patch(
            "app.services.stats.logging.connection_manager.has_subscribers",
            MagicMock(return_value=False),
            create=True,
        ) as has_subscribers,
        patch(
            "app.services.stats.logging.get_settings",
            MagicMock(
                return_value=SimpleNamespace(dashboard_update_debounce_seconds=30.0)
            ),
            create=True,
        ),
        patch(
            "app.services.stats.logging.broadcast_dashboard_update_for_request_log",
            broadcast,
        ),
    ):
        enqueue_dashboard_update_broadcast(request_log_id=101, profile_id=11)
        manager.enqueue(name="unrelated:1", run=run_unrelated)

        await manager.wait_for_idle()
        metrics = manager.metrics

    await manager.shutdown()

    has_subscribers.assert_called_once_with(profile_id=11, channel="dashboard")
    sleep_mock.assert_not_awaited()
    broadcast.assert_not_awaited()
    assert unrelated_completed.is_set() is True
    assert metrics.total_enqueued == 1
    assert metrics.total_completed == 1
    assert metrics.terminal_failures_total == 0
    assert metrics.last_failure is None


@pytest.mark.asyncio
async def test_enqueue_dashboard_update_broadcast_preserves_newer_update_while_pending_worker_waits_for_reconnect() -> (
    None
):
    enqueued_jobs = []
    broadcast = AsyncMock()
    monkeypatch_latest = {}
    monkeypatch_pending = set()
    subscriber_state = {"connected": True}

    def capture_enqueue(*, name, run, max_retries=0, retry_delay_seconds=0.0):
        enqueued_jobs.append(
            {
                "name": name,
                "run": run,
                "max_retries": max_retries,
                "retry_delay_seconds": retry_delay_seconds,
            }
        )

    def has_subscribers(*, profile_id: int, channel: str) -> bool:
        assert profile_id == 11
        assert channel == "dashboard"
        return subscriber_state["connected"]

    with (
        patch.object(
            stats_logging,
            "_dashboard_update_latest_request_log_ids",
            monkeypatch_latest,
            create=True,
        ),
        patch.object(
            stats_logging,
            "_dashboard_update_enqueued_profiles",
            monkeypatch_pending,
            create=True,
        ),
        patch(
            "app.services.stats.logging.background_task_manager.enqueue",
            MagicMock(side_effect=capture_enqueue),
        ) as enqueue,
        patch(
            "app.services.stats.logging.connection_manager.has_subscribers",
            MagicMock(side_effect=has_subscribers),
            create=True,
        ),
        patch(
            "app.services.stats.logging.broadcast_dashboard_update_for_request_log",
            broadcast,
        ),
        patch(
            "app.services.stats.logging.get_settings",
            MagicMock(
                return_value=SimpleNamespace(dashboard_update_debounce_seconds=0.0)
            ),
            create=True,
        ),
    ):
        enqueue_dashboard_update_broadcast(request_log_id=101, profile_id=11)

        subscriber_state["connected"] = False
        enqueue_dashboard_update_broadcast(request_log_id=102, profile_id=11)

        assert enqueue.call_count == 1
        assert enqueued_jobs[0]["name"] == "dashboard-update:11:101"
        assert monkeypatch_pending == {11}
        assert monkeypatch_latest == {11: 102}

        subscriber_state["connected"] = True
        await enqueued_jobs[0]["run"]()

    assert broadcast.await_args_list == [call(request_log_id=102, profile_id=11)]
    assert monkeypatch_pending == set()
    assert monkeypatch_latest == {}


@pytest.mark.asyncio
async def test_enqueue_dashboard_update_broadcast_debounce_does_not_block_shared_worker() -> (
    None
):
    manager = BackgroundTaskManager(worker_count=1)
    await manager.start()
    debounce_tasks = {}
    debounce_started = asyncio.Event()
    release_debounce = asyncio.Event()
    unrelated_completed = asyncio.Event()
    broadcast_completed = asyncio.Event()

    async def fake_sleep(_: float) -> None:
        debounce_started.set()
        await release_debounce.wait()

    async def run_unrelated() -> None:
        unrelated_completed.set()

    async def run_broadcast(*, request_log_id: int, profile_id: int) -> None:
        assert request_log_id == 101
        assert profile_id == 11
        broadcast_completed.set()

    with (
        patch("app.services.stats.logging.background_task_manager", manager),
        patch.object(
            stats_logging,
            "_dashboard_update_debounce_tasks",
            debounce_tasks,
            create=True,
        ),
        patch(
            "app.services.stats.logging.connection_manager.has_subscribers",
            MagicMock(return_value=True),
            create=True,
        ),
        patch(
            "app.services.stats.logging.get_settings",
            MagicMock(
                return_value=SimpleNamespace(dashboard_update_debounce_seconds=30.0)
            ),
            create=True,
        ),
        patch(
            "app.services.stats.logging.asyncio.sleep",
            AsyncMock(side_effect=fake_sleep),
        ),
        patch(
            "app.services.stats.logging.broadcast_dashboard_update_for_request_log",
            AsyncMock(side_effect=run_broadcast),
        ),
    ):
        try:
            enqueue_dashboard_update_broadcast(request_log_id=101, profile_id=11)
            await asyncio.wait_for(debounce_started.wait(), timeout=1)

            manager.enqueue(name="unrelated:1", run=run_unrelated)

            await asyncio.wait_for(unrelated_completed.wait(), timeout=0.1)

            release_debounce.set()
            await asyncio.wait_for(broadcast_completed.wait(), timeout=1)
            await manager.wait_for_idle()
        finally:
            release_debounce.set()
            for task in list(debounce_tasks.values()):
                task.cancel()
            if debounce_tasks:
                await asyncio.gather(*debounce_tasks.values(), return_exceptions=True)
            await manager.shutdown()


@pytest.mark.asyncio
async def test_shutdown_dashboard_update_lifecycle_cancels_and_clears_pending_debounce_tasks() -> (
    None
):
    debounce_tasks = {}
    latest_request_log_ids = {11: 101}
    enqueued_profiles = {11}
    task_started = asyncio.Event()
    task_cancelled = asyncio.Event()
    release_task = asyncio.Event()

    async def pending_debounce() -> None:
        try:
            task_started.set()
            await release_task.wait()
        except asyncio.CancelledError:
            task_cancelled.set()
            raise

    task = asyncio.create_task(pending_debounce())
    debounce_tasks[11] = task

    try:
        await asyncio.wait_for(task_started.wait(), timeout=1)
        with (
            patch.object(
                stats_logging,
                "_dashboard_update_debounce_tasks",
                debounce_tasks,
                create=True,
            ),
            patch.object(
                stats_logging,
                "_dashboard_update_latest_request_log_ids",
                latest_request_log_ids,
                create=True,
            ),
            patch.object(
                stats_logging,
                "_dashboard_update_enqueued_profiles",
                enqueued_profiles,
                create=True,
            ),
        ):
            await stats_logging.shutdown_dashboard_update_lifecycle()

        await asyncio.wait_for(task_cancelled.wait(), timeout=1)
        assert task.cancelled() is True
        assert debounce_tasks == {}
        assert latest_request_log_ids == {}
        assert enqueued_profiles == set()
    finally:
        release_task.set()
        await asyncio.gather(task, return_exceptions=True)


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
    target.connection.endpoint_rel.base_url = "https://api.openai.com"
    target.description = "Primary endpoint"
    target.upstream_url = "https://api.openai.com/v1/chat/completions"
    target.headers = {"authorization": "Bearer sk-test"}
    target.endpoint_body = b'{"model":"gpt-4o-mini"}'

    await record_attempt_audit(
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
            "app.services.loadbalancer.events.background_task_manager.enqueue",
            MagicMock(side_effect=capture_enqueue),
        ) as enqueue,
    ):
        record_failed_transition(
            event_type="opened",
            profile_id=6,
            connection_id=13,
            failure_kind="timeout",
            policy=make_failover_policy(),
            consecutive_failures=4,
            cooldown_seconds=30.0,
            blocked_until_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
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
    assert event_entry.failure_kind == "timeout"
    assert float(event_entry.cooldown_seconds) == 30.0
    assert event_entry.model_id == "gpt-4o-mini"
    assert event_entry.endpoint_id == 12
    assert event_entry.provider_id == 1


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
        patch("app.services.loadbalancer.events.background_task_manager", manager),
    ):
        record_failed_transition(
            event_type="opened",
            profile_id=6,
            connection_id=13,
            failure_kind="timeout",
            policy=make_failover_policy(),
            consecutive_failures=4,
            cooldown_seconds=30.0,
            blocked_until_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            model_id="gpt-4o-mini",
            endpoint_id=12,
            provider_id=1,
        )

        await manager.wait_for_idle()

    await manager.shutdown()

    persist_session.add.assert_called_once()
    persist_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_connection_failure_preserves_state_when_loadbalance_enqueue_fails() -> (
    None
):
    from app.models.models import LoadbalanceCurrentState

    current_state = LoadbalanceCurrentState(
        profile_id=4,
        connection_id=9,
        consecutive_failures=0,
        last_cooldown_seconds=0.0,
        probe_eligible_logged=False,
    )
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.rollback = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = current_state
    mock_session.execute = AsyncMock(side_effect=[MagicMock(), mock_result])

    with (
        patch(
            "app.services.loadbalancer.recovery.AsyncSessionLocal",
            return_value=make_session_context(mock_session),
        ),
        patch(
            "app.services.loadbalancer.events.background_task_manager.enqueue",
            MagicMock(side_effect=RuntimeError("queue unavailable")),
        ),
    ):
        await record_connection_failure(
            profile_id=4,
            connection_id=9,
            base_cooldown_seconds=30.0,
            failure_kind="timeout",
            policy=make_failover_policy(),
            model_id="gpt-4o-mini",
            endpoint_id=12,
            provider_id=1,
        )

    assert current_state.profile_id == 4
    assert current_state.connection_id == 9
    assert current_state.consecutive_failures == 1
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_connection_recovery_clears_state_when_loadbalance_enqueue_fails() -> (
    None
):
    current_state = SimpleNamespace(
        consecutive_failures=3,
        blocked_until_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        max_cooldown_strikes=0,
        ban_mode="off",
        banned_until_at=None,
        last_cooldown_seconds=30.0,
        last_failure_kind="timeout",
        probe_eligible_logged=False,
    )
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.rollback = AsyncMock()
    mock_session.delete = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = current_state
    mock_session.execute = AsyncMock(return_value=mock_result)

    with (
        patch(
            "app.services.loadbalancer.recovery.AsyncSessionLocal",
            return_value=make_session_context(mock_session),
        ),
        patch(
            "app.services.loadbalancer.events.background_task_manager.enqueue",
            MagicMock(side_effect=RuntimeError("queue unavailable")),
        ),
    ):
        await record_connection_recovery(
            profile_id=4,
            connection_id=9,
            policy=make_failover_policy(),
            model_id="gpt-4o-mini",
            endpoint_id=12,
            provider_id=1,
        )

    mock_session.delete.assert_awaited_once_with(current_state)
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_build_attempt_plan_keeps_probe_eligible_connection_when_loadbalance_enqueue_fails() -> (
    None
):
    from datetime import datetime, timezone

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
        cast(
            object,
            SimpleNamespace(
                connections=[connection],
                loadbalance_strategy=SimpleNamespace(
                    strategy_type="failover",
                    failover_recovery_enabled=True,
                ),
                model_id="gpt-4o-mini",
                provider_id=1,
            ),
        ),
    )

    current_state = SimpleNamespace(
        blocked_until_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        probe_eligible_logged=False,
    )

    with patch(
        "app.services.loadbalancer.planner.get_current_states_for_connections",
        AsyncMock(return_value={7: current_state}),
    ):
        attempt_plan = await build_attempt_plan(
            db=AsyncMock(),
            profile_id=5,
            model_config=model_config,
            now_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        )

    assert attempt_plan.connections == [connection]
    assert attempt_plan.blocked_connection_ids == []
    assert attempt_plan.probe_eligible_connection_ids == [7]


@pytest.mark.asyncio
async def test_claim_probe_eligible_stays_off_path_when_loadbalance_enqueue_fails() -> (
    None
):
    from app.services.loadbalancer.recovery import claim_probe_eligible

    with (
        patch(
            "app.services.loadbalancer.recovery.mark_probe_eligible_logged",
            AsyncMock(
                return_value={
                    "consecutive_failures": 2,
                    "blocked_until_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
                    "max_cooldown_strikes": 0,
                    "ban_mode": "off",
                    "banned_until_at": None,
                    "last_cooldown_seconds": 30.0,
                    "last_failure_kind": "timeout",
                    "probe_eligible_logged": True,
                }
            ),
        ) as mock_mark_probe_eligible_logged,
        patch(
            "app.services.loadbalancer.events.background_task_manager.enqueue",
            MagicMock(side_effect=RuntimeError("queue unavailable")),
        ),
    ):
        await claim_probe_eligible(
            profile_id=5,
            connection_id=7,
            model_id="gpt-4o-mini",
            endpoint_id=12,
            policy=make_failover_policy(),
            provider_id=1,
            now_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        )

    mock_mark_probe_eligible_logged.assert_awaited_once_with(
        profile_id=5,
        connection_id=7,
        now_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
    )


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
    enqueue = MagicMock(side_effect=RuntimeError("queue unavailable"))

    with (
        patch(
            "app.core.database.AsyncSessionLocal",
            return_value=make_session_context(mock_session),
        ),
        patch("app.services.stats.logging.background_task_manager.enqueue", enqueue),
        patch(
            "app.services.stats.logging.connection_manager.has_subscribers",
            MagicMock(return_value=True),
            create=True,
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
            endpoint_base_url="https://api.openai.com",
            status_code=500,
            response_time_ms=145,
            is_stream=False,
            request_path="/v1/chat/completions",
        )

    assert request_log_id == 654
    mock_session.commit.assert_awaited_once()
    enqueue.assert_called_once()
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
            "app.services.stats.logging.connection_manager.has_subscribers",
            MagicMock(return_value=True),
            create=True,
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
            endpoint_base_url="https://api.openai.com",
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
