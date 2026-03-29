import asyncio
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


class TestDEF078_ObservabilityMigrationTogglesUnloggedPersistence:
    @pytest.mark.asyncio
    async def test_fresh_baseline_upgrade_creates_unlogged_observability_tables(
        self, test_database_url: str
    ):
        migration_database_url = _database_url_with_name(
            test_database_url, f"prism_def078_{uuid4().hex[:12]}"
        )
        pre_current_state_tables = (
            "request_logs",
            "audit_logs",
            "loadbalance_events",
        )
        limiter_tables = (
            "connection_limiter_state",
            "connection_limiter_leases",
        )
        all_observability_tables = (
            *pre_current_state_tables,
            "loadbalance_current_state",
            *limiter_tables,
            "usage_request_events",
            "loadbalance_round_robin_state",
        )
        current_head_revision = _get_current_head_revision(migration_database_url)

        await _create_database(migration_database_url)
        try:
            await asyncio.to_thread(_upgrade_database, migration_database_url, "head")

            assert await _fetch_current_revision(migration_database_url) == [
                current_head_revision
            ]
            assert current_head_revision == "0001_prism_v9_schema_baseline"
            for table_name in all_observability_tables:
                assert (
                    await _fetch_table_persistence(migration_database_url, table_name)
                ) == "u"
        finally:
            await _drop_database(migration_database_url)
