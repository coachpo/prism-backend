import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.database import AsyncSessionLocal, get_engine
from app.core.time import utc_now
from app.main import app
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
from tests.loadbalance_strategy_helpers import make_loadbalance_strategy


def _database_url_with_name(database_url: str, database_name: str) -> str:
    split = urlsplit(database_url)
    return urlunsplit(
        (split.scheme, split.netloc, f"/{database_name}", split.query, split.fragment)
    )


def _build_alembic_config(database_url: str) -> Config:
    config = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[3] / "app" / "alembic"),
    )
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _get_current_head_revision(database_url: str) -> str:
    script = ScriptDirectory.from_config(_build_alembic_config(database_url))
    return str(script.get_current_head())


def _upgrade_database(database_url: str, revision: str) -> None:
    command.upgrade(_build_alembic_config(database_url), revision)


async def _create_database(database_url: str) -> None:
    admin_url = _database_url_with_name(database_url, "postgres")
    database_name = urlsplit(database_url).path.lstrip("/")
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{database_name}"'))
    await engine.dispose()


async def _drop_database(database_url: str) -> None:
    admin_url = _database_url_with_name(database_url, "postgres")
    database_name = urlsplit(database_url).path.lstrip("/")
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :database_name AND pid <> pg_backend_pid()"
            ),
            {"database_name": database_name},
        )
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
    await engine.dispose()


async def _fetch_current_revision(database_url: str) -> list[str]:
    engine = create_async_engine(database_url)
    async with engine.connect() as conn:
        revisions = (
            (await conn.execute(text("SELECT version_num FROM alembic_version")))
            .scalars()
            .all()
        )
    await engine.dispose()
    return [str(revision) for revision in revisions]


async def _fetch_table_columns(database_url: str, table_name: str) -> list[str]:
    engine = create_async_engine(database_url)
    async with engine.connect() as conn:
        columns = (
            (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :table_name ORDER BY ordinal_position"
                    ),
                    {"table_name": table_name},
                )
            )
            .scalars()
            .all()
        )
    await engine.dispose()
    return [str(column) for column in columns]


async def _fetch_table_persistence(database_url: str, table_name: str) -> str | None:
    engine = create_async_engine(database_url)
    async with engine.connect() as conn:
        persistence = (
            await conn.execute(
                text(
                    "SELECT relpersistence FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relname = :table_name"
                ),
                {"table_name": table_name},
            )
        ).scalar_one_or_none()
    await engine.dispose()
    if persistence is None:
        return None
    if isinstance(persistence, bytes):
        return persistence.decode()
    return str(persistence)


def _expected_latest_service_health_bucket_start(end_at: datetime) -> datetime:
    adjusted_end = end_at - timedelta(microseconds=1)
    minute = (adjusted_end.minute // 15) * 15
    return adjusted_end.replace(minute=minute, second=0, microsecond=0)


async def _seed_usage_snapshot_route_fixture() -> tuple[int, str, str]:
    suffix = uuid4().hex[:8]
    created_at = utc_now() - timedelta(hours=1)

    async with AsyncSessionLocal() as session:
        profile = Profile(
            name=f"DEF086 Usage Snapshot {suffix}",
            is_active=False,
            is_default=False,
            version=0,
        )
        vendor = Vendor(
            key=f"def086-vendor-{suffix}",
            name=f"DEF086 Vendor {suffix}",
            audit_enabled=False,
            audit_capture_bodies=False,
        )
        strategy = make_loadbalance_strategy(profile=profile, strategy_type="failover")
        model = ModelConfig(
            profile=profile,
            vendor=vendor,
            api_family="openai",
            model_id=f"def086-model-{suffix}",
            display_name=f"DEF086 Model {suffix}",
            model_type="native",
            loadbalance_strategy=strategy,
            is_enabled=True,
        )
        endpoint = Endpoint(
            profile=profile,
            name=f"DEF086 Endpoint {suffix}",
            base_url=f"https://def086-{suffix}.example.com/v1",
            api_key=f"sk-def086-{suffix}",
            position=0,
        )
        connection = Connection(
            profile=profile,
            model_config_rel=model,
            endpoint_rel=endpoint,
            is_active=True,
            priority=0,
            name=f"DEF086 Connection {suffix}",
        )
        proxy_key = ProxyApiKey(
            name=f"DEF086 Runtime Key {suffix}",
            key_prefix=f"prism_pk_def086_{suffix}",
            key_hash=(uuid4().hex * 2)[:64],
            last_four=suffix[-4:],
            is_active=True,
        )
        settings = UserSetting(
            profile=profile,
            report_currency_code="USD",
            report_currency_symbol="$",
            timezone_preference="UTC",
        )

        session.add_all(
            [
                profile,
                vendor,
                strategy,
                model,
                endpoint,
                connection,
                proxy_key,
                settings,
            ]
        )
        await session.flush()

        ingress_request_id = f"def086-ingress-{suffix}"
        session.add(
            UsageRequestEvent(
                profile_id=profile.id,
                ingress_request_id=ingress_request_id,
                model_id=model.model_id,
                resolved_target_model_id=model.model_id,
                api_family="openai",
                endpoint_id=endpoint.id,
                connection_id=connection.id,
                proxy_api_key_id=proxy_key.id,
                proxy_api_key_name_snapshot=f"DEF086 Snapshot Key {suffix}",
                status_code=200,
                success_flag=True,
                input_tokens=21,
                output_tokens=34,
                total_tokens=65,
                cache_read_input_tokens=8,
                cache_creation_input_tokens=2,
                reasoning_tokens=5,
                total_cost_original_micros=1234,
                total_cost_user_currency_micros=1234,
                currency_code_original="USD",
                report_currency_code="USD",
                report_currency_symbol="$",
                attempt_count=2,
                request_path="/v1/chat/completions",
                created_at=created_at,
            )
        )
        await session.commit()

        return profile.id, ingress_request_id, proxy_key.key_prefix


class TestDEF086_UsageStatisticsStorageContract:
    def test_schema_and_model_exports_include_usage_snapshot_contract_and_proxy_key_request_log_fields(
        self,
    ):
        from app.models.models import RequestLog, UsageRequestEvent
        from app.schemas.schemas import (
            RequestLogResponse,
            UsageRequestEventResponse,
            UsageSnapshotResponse,
        )

        request_log_columns = set(RequestLog.__table__.columns.keys())
        request_log_fields = set(RequestLogResponse.model_fields.keys())

        assert "proxy_api_key_id" in request_log_columns
        assert "proxy_api_key_name_snapshot" in request_log_columns
        assert "proxy_api_key_id" in request_log_fields
        assert "proxy_api_key_name_snapshot" in request_log_fields

        usage_request_event_columns = set(UsageRequestEvent.__table__.columns.keys())
        usage_request_event_fields = set(UsageRequestEventResponse.model_fields.keys())

        for field_name in {
            "profile_id",
            "ingress_request_id",
            "model_id",
            "resolved_target_model_id",
            "api_family",
            "endpoint_id",
            "connection_id",
            "proxy_api_key_id",
            "proxy_api_key_name_snapshot",
            "status_code",
            "success_flag",
            "attempt_count",
            "request_path",
            "created_at",
        }:
            assert field_name in usage_request_event_columns
            assert field_name in usage_request_event_fields

        for field_name in {
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
        }:
            assert field_name in UsageSnapshotResponse.model_fields
        assert "request_events" not in UsageSnapshotResponse.model_fields

    @pytest.mark.asyncio
    async def test_initial_schema_creates_usage_request_events_table_and_request_log_proxy_key_columns(
        self, test_database_url: str
    ):
        migration_database_url = _database_url_with_name(
            test_database_url, f"prism_def086_{uuid4().hex[:12]}"
        )
        expected_head_revision = _get_current_head_revision(migration_database_url)

        assert expected_head_revision != "0001_initial"

        await _create_database(migration_database_url)
        try:
            await asyncio.to_thread(_upgrade_database, migration_database_url, "head")

            assert await _fetch_current_revision(migration_database_url) == [
                expected_head_revision
            ]

            request_log_columns = await _fetch_table_columns(
                migration_database_url, "request_logs"
            )
            usage_request_event_columns = await _fetch_table_columns(
                migration_database_url, "usage_request_events"
            )
            strategy_columns = await _fetch_table_columns(
                migration_database_url, "loadbalance_strategies"
            )

            assert "proxy_api_key_id" in request_log_columns
            assert "proxy_api_key_name_snapshot" in request_log_columns
            assert usage_request_event_columns
            assert "routing_policy" in strategy_columns
            assert "auto_recovery" not in strategy_columns
            assert (
                await _fetch_table_persistence(
                    migration_database_url, "usage_request_events"
                )
            ) == "p"

            for field_name in {
                "profile_id",
                "ingress_request_id",
                "model_id",
                "resolved_target_model_id",
                "api_family",
                "endpoint_id",
                "connection_id",
                "proxy_api_key_id",
                "proxy_api_key_name_snapshot",
                "status_code",
                "success_flag",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "reasoning_tokens",
                "input_cost_micros",
                "output_cost_micros",
                "cache_read_input_cost_micros",
                "cache_creation_input_cost_micros",
                "reasoning_cost_micros",
                "total_cost_original_micros",
                "total_cost_user_currency_micros",
                "currency_code_original",
                "report_currency_code",
                "report_currency_symbol",
                "fx_rate_used",
                "fx_rate_source",
                "pricing_snapshot_unit",
                "pricing_snapshot_input",
                "pricing_snapshot_output",
                "pricing_snapshot_cache_read_input",
                "pricing_snapshot_cache_creation_input",
                "pricing_snapshot_reasoning",
                "pricing_snapshot_missing_special_token_price_policy",
                "pricing_config_version_used",
                "attempt_count",
                "request_path",
                "created_at",
            }:
                assert field_name in usage_request_event_columns
        finally:
            await _drop_database(migration_database_url)

    @pytest.mark.asyncio
    async def test_usage_snapshot_route_returns_slim_snapshot_with_proxy_key_rows(
        self,
    ):
        await get_engine().dispose()
        profile_id, ingress_request_id, _ = await _seed_usage_snapshot_route_fixture()
        transport = ASGITransport(app=app)

        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                "/api/stats/usage-snapshot",
                params={"preset": "7h"},
                headers={"X-Profile-Id": str(profile_id)},
            )

        assert response.status_code == 200
        payload = response.json()

        assert payload["time_range"]["preset"] == "7h"
        assert payload["overview"]["total_requests"] == 1
        assert payload["overview"]["cached_tokens"] == 10
        assert payload["overview"]["rolling_window_minutes"] == 30
        assert payload["overview"]["rolling_request_count"] == 0
        assert payload["overview"]["rolling_token_count"] == 0
        assert "request_events" not in payload
        assert payload["service_health"]["availability_percentage"] == 100.0
        assert payload["service_health"]["request_count"] == 1
        assert payload["service_health"]["success_count"] == 1
        assert payload["service_health"]["failed_count"] == 0
        assert payload["service_health"]["interval_minutes"] == 15
        assert "days" not in payload["service_health"]
        assert "daily" not in payload["service_health"]
        assert len(payload["service_health"]["cells"]) == 672

        service_health_bucket_starts = [
            datetime.fromisoformat(cell["bucket_start"].replace("Z", "+00:00"))
            for cell in payload["service_health"]["cells"]
        ]
        snapshot_end_at = datetime.fromisoformat(
            payload["time_range"]["end_at"].replace("Z", "+00:00")
        )

        assert service_health_bucket_starts == sorted(service_health_bucket_starts)
        assert all(
            next_bucket - current_bucket == timedelta(minutes=15)
            for current_bucket, next_bucket in zip(
                service_health_bucket_starts,
                service_health_bucket_starts[1:],
            )
        )
        assert service_health_bucket_starts[0] == service_health_bucket_starts[
            -1
        ] - timedelta(minutes=15 * 671)
        assert service_health_bucket_starts[
            -1
        ] == _expected_latest_service_health_bucket_start(snapshot_end_at)
        assert payload["endpoint_statistics"] == [
            {
                "endpoint_id": payload["endpoint_statistics"][0]["endpoint_id"],
                "endpoint_label": payload["endpoint_statistics"][0]["endpoint_label"],
                "request_count": 1,
                "success_rate": 100.0,
                "total_tokens": 65,
                "total_cost_micros": 1234,
                "models": [
                    {
                        "model_id": payload["model_statistics"][0]["model_id"],
                        "model_label": payload["model_statistics"][0]["model_label"],
                        "request_count": 1,
                        "success_rate": 100.0,
                        "total_tokens": 65,
                        "total_cost_micros": 1234,
                    }
                ],
            }
        ]
        assert payload["model_statistics"] == [
            {
                "model_id": payload["model_statistics"][0]["model_id"],
                "model_label": payload["model_statistics"][0]["model_label"],
                "request_count": 1,
                "success_rate": 100.0,
                "total_tokens": 65,
                "total_cost_micros": 1234,
            }
        ]
        assert payload["proxy_api_key_statistics"] == [
            {
                "proxy_api_key_id": payload["proxy_api_key_statistics"][0][
                    "proxy_api_key_id"
                ],
                "proxy_api_key_label": payload["proxy_api_key_statistics"][0][
                    "proxy_api_key_label"
                ],
                "request_count": 1,
                "success_rate": 100.0,
                "total_tokens": 65,
                "total_cost_micros": 1234,
            }
        ]
        assert ingress_request_id.startswith("def086-ingress-")
        assert "success_count" not in payload["endpoint_statistics"][0]
        assert "failed_count" not in payload["endpoint_statistics"][0]
        assert "success_count" not in payload["endpoint_statistics"][0]["models"][0]
        assert "failed_count" not in payload["endpoint_statistics"][0]["models"][0]
        assert "api_family" not in payload["model_statistics"][0]
        assert "success_count" not in payload["model_statistics"][0]
        assert "failed_count" not in payload["model_statistics"][0]
        assert "key_prefix" not in payload["proxy_api_key_statistics"][0]
        assert "success_count" not in payload["proxy_api_key_statistics"][0]
        assert "failed_count" not in payload["proxy_api_key_statistics"][0]
