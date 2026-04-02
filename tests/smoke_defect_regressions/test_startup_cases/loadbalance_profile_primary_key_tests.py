import asyncio
import importlib
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


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


async def _mutate_database_to_legacy_runtime_cleanup_shape(database_url: str) -> None:
    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "ALTER TABLE loadbalance_strategies DROP CONSTRAINT IF EXISTS chk_loadbalance_strategies_shape"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE loadbalance_strategies DROP CONSTRAINT IF EXISTS chk_loadbalance_strategies_legacy_strategy_type"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE loadbalance_strategies DROP CONSTRAINT IF EXISTS chk_loadbalance_strategies_type"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE loadbalance_strategies DROP COLUMN IF EXISTS legacy_strategy_type"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE loadbalance_strategies DROP COLUMN IF EXISTS routing_policy"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE routing_connection_runtime_state DROP CONSTRAINT IF EXISTS ck_rt_state_circuit_state"
            )
        )
        for column_name in [
            "probe_eligible_logged",
            "circuit_state",
            "probe_available_at",
            "live_p95_latency_ms",
            "last_live_failure_kind",
            "last_live_failure_at",
            "last_live_success_at",
            "last_probe_status",
            "last_probe_at",
            "endpoint_ping_ewma_ms",
            "conversation_delay_ewma_ms",
        ]:
            await conn.execute(
                text(
                    f"ALTER TABLE routing_connection_runtime_state DROP COLUMN IF EXISTS {column_name}"
                )
            )
        await conn.execute(
            text(
                "UPDATE alembic_version SET version_num = '0007_legacy_runtime_cleanup'"
            )
        )
    await engine.dispose()


class TestDEF075_LoadbalancePrimaryKeyContract:
    def test_dual_strategy_migration_only_recreates_round_robin_table_when_missing(
        self,
    ):
        migration = importlib.import_module(
            "app.alembic.versions.0004_dual_strategy_contract"
        )

        created_tables: list[str] = []
        created_indexes: list[str] = []

        class _Inspector:
            def get_columns(self, table_name: str):
                assert table_name == "loadbalance_strategies"
                return [{"name": "routing_policy"}]

            def has_table(self, table_name: str) -> bool:
                assert table_name == "loadbalance_round_robin_state"
                return True

        class _OpStub:
            def add_column(self, *args, **kwargs):
                return None

            def alter_column(self, *args, **kwargs):
                return None

            def execute(self, *args, **kwargs):
                return None

            def create_check_constraint(self, *args, **kwargs):
                return None

            def get_bind(self):
                return object()

            def create_table(self, name, *args, **kwargs):
                created_tables.append(name)

            def create_index(self, name, *args, **kwargs):
                created_indexes.append(name)

        original_op = migration.op
        original_inspect = migration.sa.inspect
        try:
            migration.op = _OpStub()
            migration.sa.inspect = lambda bind: _Inspector()
            migration.upgrade()
        finally:
            migration.op = original_op
            migration.sa.inspect = original_inspect

        assert created_tables == []
        assert created_indexes == []

    @pytest.mark.asyncio
    async def test_initial_schema_creates_profile_vendor_primary_keys_and_dual_strategy_columns(
        self, test_database_url: str
    ):
        drift_database_url = _database_url_with_name(
            test_database_url, f"prism_def075_{uuid4().hex[:12]}"
        )
        current_head_revision = _get_current_head_revision(drift_database_url)

        await _create_database(drift_database_url)
        try:
            await asyncio.to_thread(_upgrade_database, drift_database_url, "head")

            engine = create_async_engine(drift_database_url)
            async with engine.connect() as conn:
                version = (
                    (
                        await conn.execute(
                            text("SELECT version_num FROM alembic_version")
                        )
                    )
                    .scalars()
                    .all()
                )
                profiles_pk = (
                    (
                        await conn.execute(
                            text(
                                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'profiles'::regclass AND contype = 'p'"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                vendors_pk = (
                    (
                        await conn.execute(
                            text(
                                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'vendors'::regclass AND contype = 'p'"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                loadbalance_table = (
                    await conn.execute(
                        text("SELECT to_regclass('public.loadbalance_events')")
                    )
                ).scalar_one()
                strategy_columns = set(
                    (
                        await conn.execute(
                            text(
                                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'loadbalance_strategies'"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                connection_columns = set(
                    (
                        await conn.execute(
                            text(
                                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'connections'"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            await engine.dispose()

            assert version == [current_head_revision]
            assert current_head_revision != "0001_initial"
            assert profiles_pk == ["PRIMARY KEY (id)"]
            assert vendors_pk == ["PRIMARY KEY (id)"]
            assert loadbalance_table == "loadbalance_events"
            assert "routing_policy" in strategy_columns
            assert "strategy_type" in strategy_columns
            assert "legacy_strategy_type" in strategy_columns
            assert "auto_recovery" in strategy_columns
            assert "monitoring_probe_interval_seconds" in connection_columns
            assert "openai_probe_endpoint_variant" in connection_columns
        finally:
            await _drop_database(drift_database_url)

    @pytest.mark.asyncio
    async def test_legacy_runtime_cleanup_stamped_database_upgrades_to_current_head(
        self, test_database_url: str
    ):
        legacy_database_url = _database_url_with_name(
            test_database_url, f"prism_def075_legacy_{uuid4().hex[:12]}"
        )
        current_head_revision = _get_current_head_revision(legacy_database_url)

        await _create_database(legacy_database_url)
        try:
            await asyncio.to_thread(_upgrade_database, legacy_database_url, "head")
            await _mutate_database_to_legacy_runtime_cleanup_shape(legacy_database_url)
            await asyncio.to_thread(_upgrade_database, legacy_database_url, "head")

            engine = create_async_engine(legacy_database_url)
            async with engine.connect() as conn:
                version = (
                    (
                        await conn.execute(
                            text("SELECT version_num FROM alembic_version")
                        )
                    )
                    .scalars()
                    .all()
                )
                strategy_columns = set(
                    (
                        await conn.execute(
                            text(
                                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'loadbalance_strategies'"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                runtime_state_columns = set(
                    (
                        await conn.execute(
                            text(
                                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'routing_connection_runtime_state'"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            await engine.dispose()

            assert version == [current_head_revision]
            assert {
                "strategy_type",
                "legacy_strategy_type",
                "auto_recovery",
                "routing_policy",
            }.issubset(strategy_columns)
            assert {
                "probe_eligible_logged",
                "circuit_state",
                "probe_available_at",
                "live_p95_latency_ms",
                "last_live_failure_kind",
                "last_live_failure_at",
                "last_live_success_at",
                "last_probe_status",
                "last_probe_at",
                "endpoint_ping_ewma_ms",
                "conversation_delay_ewma_ms",
            }.issubset(runtime_state_columns)
        finally:
            await _drop_database(legacy_database_url)
