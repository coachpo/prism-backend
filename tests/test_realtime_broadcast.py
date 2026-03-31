import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from fastapi import WebSocket

import app.services.stats.logging as stats_logging
from app.models.models import ModelConfig, RequestLog
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
from tests.loadbalance_strategy_helpers import (
    DEFAULT_FAILOVER_STATUS_CODES,
    make_routing_policy_adaptive,
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
    from app.services.loadbalancer.policy import resolve_effective_loadbalance_policy

    routing_policy = make_routing_policy_adaptive(
        failure_status_codes=cast(
            list[int],
            overrides.get("failover_status_codes", DEFAULT_FAILOVER_STATUS_CODES),
        ),
        base_open_seconds=int(
            cast(float | int, overrides.get("failover_cooldown_seconds", 30.0))
        ),
        failure_threshold=cast(int, overrides.get("failover_failure_threshold", 2)),
        backoff_multiplier=float(
            cast(float | int, overrides.get("failover_backoff_multiplier", 2.0))
        ),
        max_open_seconds=cast(int, overrides.get("failover_max_cooldown_seconds", 900)),
        jitter_ratio=float(
            cast(float | int, overrides.get("failover_jitter_ratio", 0.2))
        ),
        ban_mode=cast(
            Literal["off", "temporary", "manual"],
            overrides.get("failover_ban_mode", "off"),
        ),
        max_open_strikes_before_ban=cast(
            int, overrides.get("failover_max_cooldown_strikes_before_ban", 0)
        ),
        ban_duration_seconds=cast(
            int, overrides.get("failover_ban_duration_seconds", 0)
        ),
    )
    return resolve_effective_loadbalance_policy(
        SimpleNamespace(routing_policy=routing_policy)
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
        "api_family_summary_24h": {"total_requests": 1},
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
            api_family="openai",
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
async def test_build_dashboard_update_message_preserves_full_request_log_payload() -> (
    None
):
    entry = cast(
        RequestLog,
        cast(
            object,
            SimpleNamespace(
                id=321,
                profile_id=11,
                model_id="gpt-4o-mini",
                api_family="openai",
                vendor_id=1,
                vendor_key="openai",
                vendor_name="OpenAI",
                resolved_target_model_id="gpt-4.1-mini",
                endpoint_id=4,
                connection_id=8,
                proxy_api_key_id=21,
                proxy_api_key_name_snapshot="primary-key",
                ingress_request_id="ingress-321",
                attempt_number=2,
                provider_correlation_id="provider-321",
                endpoint_base_url="https://api.openai.com",
                status_code=503,
                response_time_ms=1450,
                is_stream=False,
                input_tokens=120,
                output_tokens=80,
                total_tokens=200,
                success_flag=False,
                billable_flag=False,
                priced_flag=False,
                unpriced_reason="missing-price",
                cache_read_input_tokens=10,
                cache_creation_input_tokens=20,
                reasoning_tokens=30,
                input_cost_micros=100,
                output_cost_micros=200,
                cache_read_input_cost_micros=30,
                cache_creation_input_cost_micros=40,
                reasoning_cost_micros=50,
                total_cost_original_micros=600,
                total_cost_user_currency_micros=700,
                currency_code_original="USD",
                report_currency_code="USD",
                report_currency_symbol="$",
                fx_rate_used="1",
                fx_rate_source="manual",
                pricing_snapshot_unit="tokens",
                pricing_snapshot_input="0.1",
                pricing_snapshot_output="0.2",
                pricing_snapshot_cache_read_input="0.01",
                pricing_snapshot_cache_creation_input="0.02",
                pricing_snapshot_reasoning="0.03",
                pricing_snapshot_missing_special_token_price_policy="zero",
                pricing_config_version_used=4,
                request_path="/v1/chat/completions",
                error_detail="upstream timeout",
                endpoint_description="Primary OpenAI endpoint",
                created_at=datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc),
            ),
        ),
    )
    db = AsyncMock()

    with (
        patch(
            "app.services.stats.logging.get_stats_summary",
            AsyncMock(
                side_effect=[
                    {
                        "total_requests": 1,
                        "success_count": 0,
                        "error_count": 1,
                        "success_rate": 0.0,
                        "avg_response_time_ms": 1450.0,
                        "p95_response_time_ms": 1450,
                        "total_input_tokens": 120,
                        "total_output_tokens": 80,
                        "total_tokens": 200,
                        "groups": [],
                    },
                    {
                        "total_requests": 1,
                        "success_count": 0,
                        "error_count": 1,
                        "success_rate": 0.0,
                        "avg_response_time_ms": 1450.0,
                        "p95_response_time_ms": 1450,
                        "total_input_tokens": 120,
                        "total_output_tokens": 80,
                        "total_tokens": 200,
                        "groups": [
                            {
                                "key": "openai",
                                "total_requests": 1,
                                "success_count": 0,
                                "error_count": 1,
                                "avg_response_time_ms": 1450.0,
                                "total_tokens": 200,
                            }
                        ],
                    },
                ]
            ),
        ),
        patch(
            "app.services.stats.logging.get_spending_report",
            AsyncMock(
                return_value={
                    "summary": {
                        "total_cost_micros": 700,
                        "successful_request_count": 0,
                        "priced_request_count": 0,
                        "unpriced_request_count": 1,
                        "total_input_tokens": 120,
                        "total_output_tokens": 80,
                        "total_cache_read_input_tokens": 10,
                        "total_cache_creation_input_tokens": 20,
                        "total_reasoning_tokens": 30,
                        "total_tokens": 200,
                        "avg_cost_per_successful_request_micros": 0,
                    },
                    "groups": [],
                    "groups_total": 0,
                    "top_spending_models": [],
                    "top_spending_endpoints": [],
                    "unpriced_breakdown": {"missing-price": 1},
                    "report_currency_code": "USD",
                    "report_currency_symbol": "$",
                }
            ),
        ),
        patch(
            "app.services.stats.logging.get_throughput_stats",
            AsyncMock(
                return_value={
                    "average_rpm": 1.0,
                    "peak_rpm": 1.0,
                    "current_rpm": 1.0,
                    "total_requests": 1,
                    "time_window_seconds": 60.0,
                    "buckets": [],
                }
            ),
        ),
        patch(
            "app.services.stats.logging.build_dashboard_route_snapshot",
            AsyncMock(return_value=None),
        ),
    ):
        message = cast(
            dict[str, object],
            await stats_logging.build_dashboard_update_message(db=db, entry=entry),
        )

    request_log = cast(dict[str, object], message["request_log"])
    stats_summary = cast(dict[str, object], message["stats_summary_24h"])
    throughput_summary = cast(dict[str, object], message["throughput_24h"])
    api_family_summary = cast(dict[str, object], message["api_family_summary_24h"])
    api_family_groups = cast(list[dict[str, object]], api_family_summary["groups"])

    assert request_log["endpoint_base_url"] == "https://api.openai.com"
    assert request_log["proxy_api_key_name_snapshot"] == "primary-key"
    assert request_log["pricing_snapshot_input"] == "0.1"
    assert request_log["pricing_snapshot_reasoning"] == "0.03"
    assert request_log["report_currency_symbol"] == "$"
    assert request_log["resolved_target_model_id"] == "gpt-4.1-mini"
    assert request_log["status_code"] == 503
    assert request_log["total_tokens"] == 200
    assert stats_summary["total_requests"] == 1
    assert throughput_summary["total_requests"] == 1
    assert api_family_groups[0]["total_requests"] == 1


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
            vendor_id=1,
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
            vendor_id=1,
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
            vendor_id=1,
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
            vendor_id=1,
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
            vendor_id=1,
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
    state.setup.vendor_id = 1
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
            vendor_id=1,
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
            vendor_id=1,
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
    assert event_entry.vendor_id == 1


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
            vendor_id=1,
        )

        await manager.wait_for_idle()

    await manager.shutdown()

    persist_session.add.assert_called_once()
    persist_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_connection_failure_preserves_state_when_loadbalance_enqueue_fails() -> (
    None
):
    current_state = SimpleNamespace(
        consecutive_failures=0,
        blocked_until_at=None,
        last_failure_kind=None,
        max_cooldown_strikes=0,
        ban_mode="off",
        banned_until_at=None,
        last_cooldown_seconds=0.0,
        probe_eligible_logged=False,
    )
    persisted_snapshot = {
        "consecutive_failures": 1,
        "blocked_until_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "max_cooldown_strikes": 0,
        "ban_mode": "off",
        "banned_until_at": None,
        "last_cooldown_seconds": 30.0,
        "last_failure_kind": "timeout",
        "probe_eligible_logged": False,
    }
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.rollback = AsyncMock()

    with (
        patch(
            "app.services.loadbalancer.recovery.AsyncSessionLocal",
            return_value=make_session_context(mock_session),
        ),
        patch(
            "app.services.loadbalancer.recovery.upsert_and_lock_runtime_state",
            AsyncMock(return_value=current_state),
        ),
        patch(
            "app.services.loadbalancer.recovery.record_connection_failure_state",
            AsyncMock(return_value=persisted_snapshot),
        ) as record_state,
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
            vendor_id=1,
        )

    record_state.assert_awaited_once()
    mock_session.commit.assert_awaited_once()
    mock_session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_connection_recovery_clears_state_when_loadbalance_enqueue_fails() -> (
    None
):
    recovery_snapshot = {
        "consecutive_failures": 3,
        "blocked_until_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "max_cooldown_strikes": 0,
        "ban_mode": "off",
        "banned_until_at": None,
        "last_cooldown_seconds": 30.0,
        "last_failure_kind": "timeout",
        "probe_eligible_logged": False,
    }
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.rollback = AsyncMock()

    with (
        patch(
            "app.services.loadbalancer.recovery.AsyncSessionLocal",
            return_value=make_session_context(mock_session),
        ),
        patch(
            "app.services.loadbalancer.recovery.record_connection_recovery_state",
            AsyncMock(return_value=recovery_snapshot),
        ) as record_state,
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
            vendor_id=1,
        )

    record_state.assert_awaited_once_with(
        session=mock_session,
        profile_id=4,
        connection_id=9,
    )
    mock_session.commit.assert_awaited_once()
    mock_session.rollback.assert_not_awaited()


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
        qps_limit=None,
        max_in_flight_non_stream=None,
        max_in_flight_stream=None,
        name="connection-7",
    )
    model_config = cast(
        ModelConfig,
        cast(
            object,
            SimpleNamespace(
                connections=[connection],
                loadbalance_strategy=SimpleNamespace(
                    routing_policy=make_routing_policy_adaptive()
                ),
                model_id="gpt-4o-mini",
                vendor_id=1,
            ),
        ),
    )

    current_state = SimpleNamespace(
        circuit_state="open",
        blocked_until_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        probe_eligible_logged=False,
    )

    with patch(
        "app.services.loadbalancer.planner.get_runtime_states_for_connections",
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
            vendor_id=1,
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
            api_family="openai",
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
            api_family="openai",
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
