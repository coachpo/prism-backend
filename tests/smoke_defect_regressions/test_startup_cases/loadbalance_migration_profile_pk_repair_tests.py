import asyncio
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
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


class TestDEF075_LoadbalanceMigrationRepairsLegacyOwnerPrimaryKeys:
    @pytest.mark.asyncio
    async def test_fresh_baseline_upgrade_creates_profile_vendor_primary_keys_and_v9_strategy_storage(
        self, test_database_url: str
    ):
        drift_database_url = _database_url_with_name(
            test_database_url, f"prism_def075_{uuid4().hex[:12]}"
        )

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
            await engine.dispose()

            assert version == ["0001_prism_v9_schema_baseline"]
            assert profiles_pk == ["PRIMARY KEY (id)"]
            assert vendors_pk == ["PRIMARY KEY (id)"]
            assert loadbalance_table == "loadbalance_events"
            assert "auto_recovery" in strategy_columns
            assert "failover_recovery_enabled" not in strategy_columns
        finally:
            await _drop_database(drift_database_url)
