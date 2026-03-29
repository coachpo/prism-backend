from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

from app.core.database import AsyncSessionLocal
from app.models.models import (
    Connection,
    Endpoint,
    LoadbalanceStrategy,
    ModelConfig,
    Profile,
    RoutingConnectionRuntimeState,
    UserSetting,
    Vendor,
)
from tests.loadbalance_strategy_helpers import make_routing_policy_adaptive


def _load_module(module_name: str):
    try:
        return importlib.import_module(module_name)
    except Exception as exc:
        pytest.fail(f"{module_name} must exist for monitoring scheduler: {exc}")


async def _seed_scheduler_fixture(*, now_at: datetime) -> dict[str, int]:
    suffix = uuid4().hex[:8]

    async with AsyncSessionLocal() as session:
        vendor = Vendor(
            key=f"monitoring-scheduler-{suffix}",
            name=f"Monitoring Scheduler {suffix}",
            audit_enabled=False,
            audit_capture_bodies=False,
        )
        profile = Profile(
            name=f"Monitoring Scheduler Profile {suffix}",
            is_active=False,
            version=0,
        )
        endpoint = Endpoint(
            profile=profile,
            name=f"scheduler-endpoint-{suffix}",
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            position=0,
        )
        strategy = LoadbalanceStrategy(
            profile=profile,
            name=f"scheduler-strategy-{suffix}",
            routing_policy=make_routing_policy_adaptive(),
        )
        model = ModelConfig(
            vendor=vendor,
            profile=profile,
            api_family="openai",
            model_id=f"model-{suffix}",
            model_type="native",
            loadbalance_strategy=strategy,
            is_enabled=True,
        )
        settings = UserSetting(
            profile=profile,
            report_currency_code="USD",
            report_currency_symbol="$",
            monitoring_probe_interval_seconds=120,
        )
        due_connection = Connection(
            profile=profile,
            model_config_rel=model,
            endpoint_rel=endpoint,
            is_active=True,
            priority=0,
            name=f"due-{suffix}",
        )
        not_due_connection = Connection(
            profile=profile,
            model_config_rel=model,
            endpoint_rel=endpoint,
            is_active=True,
            priority=1,
            name=f"not-due-{suffix}",
        )
        inactive_connection = Connection(
            profile=profile,
            model_config_rel=model,
            endpoint_rel=endpoint,
            is_active=False,
            priority=2,
            name=f"inactive-{suffix}",
        )
        eligible_open_connection = Connection(
            profile=profile,
            model_config_rel=model,
            endpoint_rel=endpoint,
            is_active=True,
            priority=3,
            name=f"eligible-open-{suffix}",
        )
        session.add_all(
            [
                vendor,
                profile,
                endpoint,
                strategy,
                model,
                settings,
                due_connection,
                not_due_connection,
                inactive_connection,
                eligible_open_connection,
            ]
        )
        await session.flush()

        session.add_all(
            [
                RoutingConnectionRuntimeState(
                    profile_id=profile.id,
                    connection_id=due_connection.id,
                    last_probe_status="healthy",
                    last_probe_at=now_at - timedelta(seconds=121),
                    circuit_state="closed",
                ),
                RoutingConnectionRuntimeState(
                    profile_id=profile.id,
                    connection_id=not_due_connection.id,
                    last_probe_status="healthy",
                    last_probe_at=now_at - timedelta(seconds=30),
                    circuit_state="closed",
                ),
                RoutingConnectionRuntimeState(
                    profile_id=profile.id,
                    connection_id=inactive_connection.id,
                    last_probe_status="healthy",
                    last_probe_at=now_at - timedelta(seconds=121),
                    circuit_state="closed",
                ),
                RoutingConnectionRuntimeState(
                    profile_id=profile.id,
                    connection_id=eligible_open_connection.id,
                    last_probe_status="unhealthy",
                    last_probe_at=now_at - timedelta(seconds=15),
                    circuit_state="open",
                    blocked_until_at=now_at - timedelta(seconds=1),
                    probe_available_at=now_at - timedelta(seconds=1),
                ),
            ]
        )
        await session.commit()
        return {
            "profile_id": profile.id,
            "due_connection_id": due_connection.id,
            "not_due_connection_id": not_due_connection.id,
            "inactive_connection_id": inactive_connection.id,
            "eligible_open_connection_id": eligible_open_connection.id,
        }


class TestMonitoringScheduler:
    @pytest.mark.asyncio
    async def test_run_monitoring_cycle_uses_persisted_cadence_and_probes_only_due_or_probe_eligible_connections(
        self,
    ):
        scheduler_module = _load_module("app.services.monitoring.scheduler")
        run_monitoring_cycle = getattr(scheduler_module, "run_monitoring_cycle", None)
        assert run_monitoring_cycle is not None, (
            "app.services.monitoring.scheduler.run_monitoring_cycle must exist"
        )

        now_at = datetime(2026, 3, 29, 16, 0, tzinfo=timezone.utc)
        fixture = await _seed_scheduler_fixture(now_at=now_at)
        probe_mock = AsyncMock()

        await run_monitoring_cycle(
            http_client=AsyncMock(),
            now_at=now_at,
            run_connection_probe_fn=probe_mock,
        )

        probed_connection_ids = [
            call.kwargs["connection_id"]
            for call in probe_mock.await_args_list
            if call.kwargs["profile_id"] == fixture["profile_id"]
        ]
        assert probed_connection_ids == [
            fixture["due_connection_id"],
            fixture["eligible_open_connection_id"],
        ]

    @pytest.mark.asyncio
    async def test_monitoring_scheduler_start_and_stop_manage_loop_lifecycle(self):
        scheduler_module = _load_module("app.services.monitoring.scheduler")
        MonitoringScheduler = getattr(
            scheduler_module,
            "MonitoringScheduler",
            None,
        )
        assert MonitoringScheduler is not None, (
            "app.services.monitoring.scheduler.MonitoringScheduler must exist"
        )

        cycle_calls: list[int] = []
        stop_called = asyncio.Event()

        async def run_cycle_fn(
            *, http_client, now_at=None, run_connection_probe_fn=None
        ):
            _ = http_client
            _ = now_at
            _ = run_connection_probe_fn
            cycle_calls.append(1)

        async def sleep_fn(seconds: float) -> None:
            _ = seconds
            stop_called.set()
            await asyncio.sleep(0)

        scheduler = MonitoringScheduler(
            http_client=AsyncMock(),
            run_cycle_fn=run_cycle_fn,
            sleep_fn=sleep_fn,
        )

        await scheduler.start()
        await asyncio.wait_for(stop_called.wait(), timeout=1.0)
        await scheduler.stop()

        assert cycle_calls == [1]
        assert getattr(scheduler, "started", False) is False
