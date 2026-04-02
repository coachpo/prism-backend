from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from uuid import uuid4

import pytest
from unittest.mock import patch

from app.core.database import AsyncSessionLocal
from app.models.models import (
    Connection,
    Endpoint,
    ModelConfig,
    Profile,
    ProxyApiKey,
    UsageRequestEvent,
    UserSetting,
    Vendor,
)
from app.schemas.domains.usage_statistics import (
    UsageEndpointModelStatistic,
    UsageEndpointStatistic,
    UsageModelStatistic,
    UsageProxyApiKeyStatistic,
    UsageServiceHealth,
    UsageSnapshotResponse,
)
from app.services import stats_service
from app.services.stats import usage_snapshot as usage_snapshot_module
from app.services.stats.time_presets import resolve_time_preset
from tests.loadbalance_strategy_helpers import make_loadbalance_strategy


@dataclass(slots=True)
class UsageSnapshotSeed:
    alt_model_id: str
    primary_endpoint_id: int
    primary_key_id: int
    primary_model_id: str
    profile_id: int
    secondary_endpoint_id: int


async def _seed_usage_snapshot_dataset(now: datetime) -> UsageSnapshotSeed:
    suffix = uuid4().hex[:8]

    async with AsyncSessionLocal() as session:
        primary_profile = Profile(
            name=f"Usage Snapshot Primary {suffix}",
            is_active=False,
            is_default=False,
            version=0,
        )
        secondary_profile = Profile(
            name=f"Usage Snapshot Secondary {suffix}",
            is_active=False,
            is_default=False,
            version=0,
        )
        vendor = Vendor(
            key=f"usage-snapshot-vendor-{suffix}",
            name=f"Usage Snapshot Vendor {suffix}",
            audit_enabled=False,
            audit_capture_bodies=False,
        )
        primary_strategy = make_loadbalance_strategy(
            profile=primary_profile,
            strategy_type="failover",
        )
        secondary_strategy = make_loadbalance_strategy(
            profile=secondary_profile,
            strategy_type="failover",
        )

        primary_model = ModelConfig(
            profile=primary_profile,
            vendor=vendor,
            api_family="openai",
            model_id=f"gpt-4o-{suffix}",
            display_name=f"GPT 4o {suffix}",
            model_type="native",
            loadbalance_strategy=primary_strategy,
            is_enabled=True,
        )
        alt_model = ModelConfig(
            profile=primary_profile,
            vendor=vendor,
            api_family="anthropic",
            model_id=f"claude-3-7-sonnet-{suffix}",
            display_name=None,
            model_type="native",
            loadbalance_strategy=primary_strategy,
            is_enabled=True,
        )
        secondary_model = ModelConfig(
            profile=secondary_profile,
            vendor=vendor,
            api_family="openai",
            model_id=f"gpt-4.1-mini-{suffix}",
            display_name=f"Secondary Model {suffix}",
            model_type="native",
            loadbalance_strategy=secondary_strategy,
            is_enabled=True,
        )

        primary_endpoint = Endpoint(
            profile=primary_profile,
            name=f"Primary Endpoint {suffix}",
            base_url=f"https://primary-{suffix}.example.com/v1",
            api_key=f"sk-primary-{suffix}",
            position=0,
        )
        secondary_endpoint = Endpoint(
            profile=primary_profile,
            name=f"Secondary Endpoint {suffix}",
            base_url=f"https://secondary-{suffix}.example.com/v1",
            api_key=f"sk-secondary-{suffix}",
            position=1,
        )
        other_profile_endpoint = Endpoint(
            profile=secondary_profile,
            name=f"Other Endpoint {suffix}",
            base_url=f"https://other-{suffix}.example.com/v1",
            api_key=f"sk-other-{suffix}",
            position=0,
        )

        primary_connection = Connection(
            profile=primary_profile,
            model_config_rel=primary_model,
            endpoint_rel=primary_endpoint,
            is_active=True,
            priority=0,
            name=f"Primary Connection {suffix}",
        )
        secondary_connection = Connection(
            profile=primary_profile,
            model_config_rel=alt_model,
            endpoint_rel=secondary_endpoint,
            is_active=True,
            priority=0,
            name=f"Secondary Connection {suffix}",
        )
        other_profile_connection = Connection(
            profile=secondary_profile,
            model_config_rel=secondary_model,
            endpoint_rel=other_profile_endpoint,
            is_active=True,
            priority=0,
            name=f"Other Connection {suffix}",
        )

        primary_key = ProxyApiKey(
            name=f"Primary Runtime Key {suffix}",
            key_prefix=f"prism_pk_primary_{suffix}",
            key_hash=(uuid4().hex * 2)[:64],
            last_four=suffix[-4:],
            is_active=True,
        )
        other_profile_key = ProxyApiKey(
            name=f"Other Runtime Key {suffix}",
            key_prefix=f"prism_pk_other_{suffix}",
            key_hash=(uuid4().hex * 2)[:64],
            last_four=suffix[:4],
            is_active=True,
        )

        primary_settings = UserSetting(
            profile=primary_profile,
            report_currency_code="USD",
            report_currency_symbol="$",
            timezone_preference="UTC",
        )
        secondary_settings = UserSetting(
            profile=secondary_profile,
            report_currency_code="EUR",
            report_currency_symbol="€",
            timezone_preference="UTC",
        )

        session.add_all(
            [
                primary_profile,
                secondary_profile,
                vendor,
                primary_strategy,
                secondary_strategy,
                primary_model,
                alt_model,
                secondary_model,
                primary_endpoint,
                secondary_endpoint,
                other_profile_endpoint,
                primary_connection,
                secondary_connection,
                other_profile_connection,
                primary_key,
                other_profile_key,
                primary_settings,
                secondary_settings,
            ]
        )
        await session.flush()

        session.add_all(
            [
                UsageRequestEvent(
                    profile_id=primary_profile.id,
                    ingress_request_id=f"ingress-success-{suffix}",
                    model_id=primary_model.model_id,
                    resolved_target_model_id=primary_model.model_id,
                    api_family="openai",
                    endpoint_id=primary_endpoint.id,
                    connection_id=primary_connection.id,
                    proxy_api_key_id=primary_key.id,
                    proxy_api_key_name_snapshot="Snapshot Primary Key",
                    status_code=200,
                    success_flag=True,
                    input_tokens=100,
                    output_tokens=50,
                    total_tokens=185,
                    cache_read_input_tokens=20,
                    cache_creation_input_tokens=5,
                    reasoning_tokens=10,
                    input_cost_micros=1000,
                    output_cost_micros=2000,
                    cache_read_input_cost_micros=300,
                    cache_creation_input_cost_micros=400,
                    reasoning_cost_micros=500,
                    total_cost_original_micros=4200,
                    total_cost_user_currency_micros=4200,
                    currency_code_original="USD",
                    report_currency_code="USD",
                    report_currency_symbol="$",
                    attempt_count=3,
                    request_path="/v1/chat/completions",
                    created_at=now - timedelta(minutes=15),
                ),
                UsageRequestEvent(
                    profile_id=primary_profile.id,
                    ingress_request_id=f"ingress-failure-{suffix}",
                    model_id=alt_model.model_id,
                    resolved_target_model_id=alt_model.model_id,
                    api_family="anthropic",
                    endpoint_id=secondary_endpoint.id,
                    connection_id=secondary_connection.id,
                    proxy_api_key_id=primary_key.id,
                    proxy_api_key_name_snapshot="Snapshot Primary Key",
                    status_code=500,
                    success_flag=False,
                    input_tokens=40,
                    output_tokens=20,
                    total_tokens=60,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                    reasoning_tokens=0,
                    total_cost_original_micros=None,
                    total_cost_user_currency_micros=None,
                    currency_code_original=None,
                    report_currency_code="USD",
                    report_currency_symbol="$",
                    attempt_count=1,
                    request_path="/v1/messages",
                    created_at=now - timedelta(hours=2),
                ),
                UsageRequestEvent(
                    profile_id=primary_profile.id,
                    ingress_request_id=f"ingress-older-{suffix}",
                    model_id=primary_model.model_id,
                    resolved_target_model_id=primary_model.model_id,
                    api_family="openai",
                    endpoint_id=primary_endpoint.id,
                    connection_id=primary_connection.id,
                    proxy_api_key_id=primary_key.id,
                    proxy_api_key_name_snapshot="Snapshot Primary Key",
                    status_code=200,
                    success_flag=True,
                    input_tokens=120,
                    output_tokens=30,
                    total_tokens=150,
                    cache_read_input_tokens=10,
                    cache_creation_input_tokens=0,
                    reasoning_tokens=5,
                    total_cost_original_micros=1500,
                    total_cost_user_currency_micros=1500,
                    currency_code_original="USD",
                    report_currency_code="USD",
                    report_currency_symbol="$",
                    attempt_count=1,
                    request_path="/v1/chat/completions",
                    created_at=now - timedelta(hours=10),
                ),
                UsageRequestEvent(
                    profile_id=primary_profile.id,
                    ingress_request_id=f"ingress-week-{suffix}",
                    model_id=alt_model.model_id,
                    resolved_target_model_id=alt_model.model_id,
                    api_family="anthropic",
                    endpoint_id=secondary_endpoint.id,
                    connection_id=secondary_connection.id,
                    proxy_api_key_id=None,
                    proxy_api_key_name_snapshot=None,
                    status_code=200,
                    success_flag=True,
                    input_tokens=70,
                    output_tokens=10,
                    total_tokens=80,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                    reasoning_tokens=3,
                    total_cost_original_micros=800,
                    total_cost_user_currency_micros=800,
                    currency_code_original="USD",
                    report_currency_code="USD",
                    report_currency_symbol="$",
                    attempt_count=1,
                    request_path="/v1/messages",
                    created_at=now - timedelta(days=2),
                ),
                UsageRequestEvent(
                    profile_id=secondary_profile.id,
                    ingress_request_id=f"ingress-other-profile-{suffix}",
                    model_id=secondary_model.model_id,
                    resolved_target_model_id=secondary_model.model_id,
                    api_family="openai",
                    endpoint_id=other_profile_endpoint.id,
                    connection_id=other_profile_connection.id,
                    proxy_api_key_id=other_profile_key.id,
                    proxy_api_key_name_snapshot="Other Snapshot Key",
                    status_code=200,
                    success_flag=True,
                    input_tokens=999,
                    output_tokens=1,
                    total_tokens=1000,
                    cache_read_input_tokens=100,
                    cache_creation_input_tokens=50,
                    reasoning_tokens=25,
                    total_cost_original_micros=5000,
                    total_cost_user_currency_micros=5000,
                    currency_code_original="EUR",
                    report_currency_code="EUR",
                    report_currency_symbol="€",
                    attempt_count=1,
                    request_path="/v1/chat/completions",
                    created_at=now - timedelta(hours=1),
                ),
            ]
        )
        await session.commit()

        return UsageSnapshotSeed(
            alt_model_id=alt_model.model_id,
            primary_endpoint_id=primary_endpoint.id,
            primary_key_id=primary_key.id,
            primary_model_id=primary_model.model_id,
            profile_id=primary_profile.id,
            secondary_endpoint_id=secondary_endpoint.id,
        )


class TestUsageSnapshotService:
    def test_usage_snapshot_response_exposes_slim_page_contract(self):
        expected_fields = {
            "generated_at",
            "time_range",
            "currency",
            "overview",
            "service_health",
            "request_trends",
            "token_usage_trends",
            "token_type_breakdown",
            "cost_overview",
            "endpoint_statistics",
            "model_statistics",
            "proxy_api_key_statistics",
        }

        assert set(UsageSnapshotResponse.model_fields.keys()) == expected_fields
        assert set(UsageEndpointStatistic.model_fields.keys()) == {
            "endpoint_id",
            "endpoint_label",
            "request_count",
            "success_rate",
            "total_tokens",
            "total_cost_micros",
            "models",
        }
        assert set(UsageEndpointModelStatistic.model_fields.keys()) == {
            "model_id",
            "model_label",
            "request_count",
            "success_rate",
            "total_tokens",
            "total_cost_micros",
        }
        assert set(UsageModelStatistic.model_fields.keys()) == {
            "model_id",
            "model_label",
            "request_count",
            "success_rate",
            "total_tokens",
            "total_cost_micros",
        }
        assert set(UsageProxyApiKeyStatistic.model_fields.keys()) == {
            "proxy_api_key_id",
            "proxy_api_key_label",
            "request_count",
            "success_rate",
            "total_tokens",
            "total_cost_micros",
        }
        assert set(UsageServiceHealth.model_fields.keys()) == {
            "availability_percentage",
            "request_count",
            "success_count",
            "failed_count",
            "interval_minutes",
            "cells",
        }

    def test_resolve_time_preset_supports_last_seven_hours(self):
        fixed_end = datetime(2026, 3, 27, 12, 0, tzinfo=timezone.utc)

        from_time, to_time = resolve_time_preset("7h", None, fixed_end)

        assert from_time == fixed_end - timedelta(hours=7)
        assert to_time == fixed_end

    def test_latest_service_health_bucket_uses_live_interval_when_now_not_on_boundary(
        self,
    ):
        fixed_end = datetime(2026, 3, 27, 12, 7, tzinfo=timezone.utc)

        latest_bucket_start = usage_snapshot_module._latest_service_health_bucket_start(
            fixed_end
        )

        assert latest_bucket_start == datetime(2026, 3, 27, 12, 0, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_get_usage_snapshot_aggregates_usage_request_events_for_selected_profile(
        self,
    ):
        get_usage_snapshot = cast(
            Callable[..., Awaitable[dict[str, object]]] | None,
            getattr(stats_service, "get_usage_snapshot", None),
        )
        assert callable(get_usage_snapshot), (
            "stats_service.get_usage_snapshot must exist"
        )

        fixed_now = datetime(2026, 3, 27, 12, 0, tzinfo=timezone.utc)
        seed = await _seed_usage_snapshot_dataset(fixed_now)

        with patch("app.services.stats.usage_snapshot.utc_now", return_value=fixed_now):
            async with AsyncSessionLocal() as session:
                snapshot = cast(
                    dict[str, Any],
                    await get_usage_snapshot(
                        session,
                        profile_id=seed.profile_id,
                        preset="7h",
                    ),
                )

        assert snapshot["time_range"] == {
            "preset": "7h",
            "start_at": fixed_now - timedelta(hours=7),
            "end_at": fixed_now,
        }
        assert snapshot["currency"] == {"code": "USD", "symbol": "$"}
        assert snapshot["overview"] == {
            "total_requests": 2,
            "success_requests": 1,
            "failed_requests": 1,
            "success_rate": 50.0,
            "total_tokens": 245,
            "input_tokens": 140,
            "output_tokens": 70,
            "cached_tokens": 25,
            "reasoning_tokens": 10,
            "average_rpm": 0.005,
            "average_tpm": 0.583,
            "total_cost_micros": 4200,
            "rolling_window_minutes": 30,
            "rolling_request_count": 1,
            "rolling_token_count": 185,
            "rolling_rpm": 0.033,
            "rolling_tpm": 6.167,
        }
        assert snapshot["service_health"]["availability_percentage"] == 75.0
        assert snapshot["service_health"]["request_count"] == 4
        assert snapshot["service_health"]["success_count"] == 3
        assert snapshot["service_health"]["failed_count"] == 1
        assert snapshot["service_health"]["interval_minutes"] == 15
        assert "days" not in snapshot["service_health"]
        assert "daily" not in snapshot["service_health"]

        service_health_cells_list = snapshot["service_health"]["cells"]
        assert len(service_health_cells_list) == 672
        assert service_health_cells_list[0]["bucket_start"] == datetime(
            2026, 3, 20, 12, 0, tzinfo=timezone.utc
        )
        assert service_health_cells_list[-1]["bucket_start"] == datetime(
            2026, 3, 27, 11, 45, tzinfo=timezone.utc
        )
        assert all(
            right["bucket_start"] - left["bucket_start"] == timedelta(minutes=15)
            for left, right in zip(
                service_health_cells_list, service_health_cells_list[1:]
            )
        )

        service_health_cells = {
            cell["bucket_start"]: cell for cell in service_health_cells_list
        }
        assert service_health_cells[
            datetime(2026, 3, 27, 11, 45, tzinfo=timezone.utc)
        ] == {
            "bucket_start": datetime(2026, 3, 27, 11, 45, tzinfo=timezone.utc),
            "request_count": 1,
            "success_count": 1,
            "failed_count": 0,
            "availability_percentage": 100.0,
            "status": "ok",
        }
        assert service_health_cells[
            datetime(2026, 3, 27, 10, 0, tzinfo=timezone.utc)
        ] == {
            "bucket_start": datetime(2026, 3, 27, 10, 0, tzinfo=timezone.utc),
            "request_count": 1,
            "success_count": 0,
            "failed_count": 1,
            "availability_percentage": 0.0,
            "status": "down",
        }
        assert service_health_cells[
            datetime(2026, 3, 27, 10, 15, tzinfo=timezone.utc)
        ] == {
            "bucket_start": datetime(2026, 3, 27, 10, 15, tzinfo=timezone.utc),
            "request_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "availability_percentage": None,
            "status": "empty",
        }
        assert service_health_cells[
            datetime(2026, 3, 27, 2, 0, tzinfo=timezone.utc)
        ] == {
            "bucket_start": datetime(2026, 3, 27, 2, 0, tzinfo=timezone.utc),
            "request_count": 1,
            "success_count": 1,
            "failed_count": 0,
            "availability_percentage": 100.0,
            "status": "ok",
        }
        assert service_health_cells[
            datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc)
        ] == {
            "bucket_start": datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc),
            "request_count": 1,
            "success_count": 1,
            "failed_count": 0,
            "availability_percentage": 100.0,
            "status": "ok",
        }

        request_trend_series = {
            series["key"]: series for series in snapshot["request_trends"]["hourly"]
        }
        assert set(request_trend_series) == {
            "all",
            seed.primary_model_id,
            seed.alt_model_id,
        }
        all_request_points = {
            point["bucket_start"]: point
            for point in request_trend_series["all"]["points"]
        }
        assert all_request_points[datetime(2026, 3, 27, 9, 0, tzinfo=timezone.utc)] == {
            "bucket_start": datetime(2026, 3, 27, 9, 0, tzinfo=timezone.utc),
            "request_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "rpm": 0.0,
        }
        assert (
            all_request_points[datetime(2026, 3, 27, 10, 0, tzinfo=timezone.utc)][
                "request_count"
            ]
            == 1
        )
        assert (
            all_request_points[datetime(2026, 3, 27, 11, 0, tzinfo=timezone.utc)][
                "request_count"
            ]
            == 1
        )

        token_usage_series = {
            series["key"]: series for series in snapshot["token_usage_trends"]["hourly"]
        }
        assert token_usage_series[seed.primary_model_id]["total_tokens"] == 185
        assert token_usage_series[seed.alt_model_id]["total_tokens"] == 60

        token_type_points = {
            point["bucket_start"]: point
            for point in snapshot["token_type_breakdown"]["hourly"]
        }
        assert token_type_points[datetime(2026, 3, 27, 11, 0, tzinfo=timezone.utc)] == {
            "bucket_start": datetime(2026, 3, 27, 11, 0, tzinfo=timezone.utc),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 25,
            "reasoning_tokens": 10,
        }

        assert snapshot["cost_overview"] == {
            "total_cost_micros": 4200,
            "priced_request_count": 1,
            "unpriced_request_count": 0,
            "hourly": [
                {
                    "bucket_start": datetime(2026, 3, 27, 5, 0, tzinfo=timezone.utc),
                    "total_cost_micros": 0,
                },
                {
                    "bucket_start": datetime(2026, 3, 27, 6, 0, tzinfo=timezone.utc),
                    "total_cost_micros": 0,
                },
                {
                    "bucket_start": datetime(2026, 3, 27, 7, 0, tzinfo=timezone.utc),
                    "total_cost_micros": 0,
                },
                {
                    "bucket_start": datetime(2026, 3, 27, 8, 0, tzinfo=timezone.utc),
                    "total_cost_micros": 0,
                },
                {
                    "bucket_start": datetime(2026, 3, 27, 9, 0, tzinfo=timezone.utc),
                    "total_cost_micros": 0,
                },
                {
                    "bucket_start": datetime(2026, 3, 27, 10, 0, tzinfo=timezone.utc),
                    "total_cost_micros": 0,
                },
                {
                    "bucket_start": datetime(2026, 3, 27, 11, 0, tzinfo=timezone.utc),
                    "total_cost_micros": 4200,
                },
                {
                    "bucket_start": datetime(2026, 3, 27, 12, 0, tzinfo=timezone.utc),
                    "total_cost_micros": 0,
                },
            ],
            "daily": [
                {
                    "bucket_start": datetime(2026, 3, 27, 0, 0, tzinfo=timezone.utc),
                    "total_cost_micros": 4200,
                }
            ],
        }

        endpoint_rows = {
            row["endpoint_id"]: row for row in snapshot["endpoint_statistics"]
        }
        assert endpoint_rows[seed.primary_endpoint_id]["request_count"] == 1
        assert endpoint_rows[seed.primary_endpoint_id]["success_rate"] == 100.0
        assert endpoint_rows[seed.primary_endpoint_id]["endpoint_label"].startswith(
            "Primary Endpoint"
        )
        assert endpoint_rows[seed.primary_endpoint_id]["total_cost_micros"] == 4200
        assert endpoint_rows[seed.secondary_endpoint_id]["request_count"] == 1
        assert endpoint_rows[seed.secondary_endpoint_id]["success_rate"] == 0.0
        assert "success_count" not in endpoint_rows[seed.primary_endpoint_id]
        assert "failed_count" not in endpoint_rows[seed.primary_endpoint_id]
        assert (
            "success_count" not in endpoint_rows[seed.primary_endpoint_id]["models"][0]
        )
        assert (
            "failed_count" not in endpoint_rows[seed.primary_endpoint_id]["models"][0]
        )

        model_rows = {row["model_id"]: row for row in snapshot["model_statistics"]}
        assert model_rows[seed.primary_model_id]["model_label"].startswith("GPT 4o")
        assert model_rows[seed.alt_model_id]["model_label"] == seed.alt_model_id
        assert model_rows[seed.primary_model_id]["request_count"] == 1
        assert model_rows[seed.alt_model_id]["request_count"] == 1
        assert "api_family" not in model_rows[seed.primary_model_id]
        assert "success_count" not in model_rows[seed.primary_model_id]
        assert "failed_count" not in model_rows[seed.primary_model_id]
        assert "request_events" not in snapshot

        assert snapshot["proxy_api_key_statistics"] == [
            {
                "proxy_api_key_id": seed.primary_key_id,
                "proxy_api_key_label": "Snapshot Primary Key",
                "request_count": 2,
                "success_rate": 50.0,
                "total_tokens": 245,
                "total_cost_micros": 4200,
            }
        ]
