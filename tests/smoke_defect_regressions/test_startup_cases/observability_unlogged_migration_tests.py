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
        str(Path(__file__).resolve().parents[3] / "alembic"),
    )
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _get_current_head_revision(database_url: str) -> str:
    script = ScriptDirectory.from_config(_build_alembic_config(database_url))
    return str(script.get_current_head())


def _upgrade_database(database_url: str, revision: str) -> None:
    command.upgrade(_build_alembic_config(database_url), revision)


def _downgrade_database(database_url: str, revision: str) -> None:
    command.downgrade(_build_alembic_config(database_url), revision)


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


async def _fetch_table_persistence(database_url: str, table_name: str) -> str:
    engine = create_async_engine(database_url)
    async with engine.connect() as conn:
        persistence = (
            await conn.execute(
                text(
                    "SELECT relpersistence FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relname = :table_name"
                ),
                {"table_name": table_name},
            )
        ).scalar_one()
    await engine.dispose()
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
    async def test_upgrade_and_downgrade_flip_observability_tables_between_logged_and_unlogged(
        self, test_database_url: str
    ):
        migration_database_url = _database_url_with_name(
            test_database_url, f"prism_def078_{uuid4().hex[:12]}"
        )
        observability_tables = (
            "request_logs",
            "audit_logs",
            "loadbalance_events",
        )
        current_head_revision = _get_current_head_revision(migration_database_url)

        await _create_database(migration_database_url)
        try:
            await asyncio.to_thread(
                _upgrade_database, migration_database_url, "0010_webauthn_challenges"
            )
            await asyncio.to_thread(
                _upgrade_database,
                migration_database_url,
                "0011_observability_unlogged",
            )

            assert len("0011_observability_unlogged") <= 32
            assert await _fetch_current_revision(migration_database_url) == [
                "0011_observability_unlogged"
            ]
            for table_name in observability_tables:
                assert (
                    await _fetch_table_persistence(migration_database_url, table_name)
                ) == "u"

            await asyncio.to_thread(
                _downgrade_database, migration_database_url, "0010_webauthn_challenges"
            )

            assert await _fetch_current_revision(migration_database_url) == [
                "0010_webauthn_challenges"
            ]
            for table_name in observability_tables:
                assert (
                    await _fetch_table_persistence(migration_database_url, table_name)
                ) == "p"

            await asyncio.to_thread(_upgrade_database, migration_database_url, "head")

            assert await _fetch_current_revision(migration_database_url) == [
                current_head_revision
            ]
            for table_name in observability_tables:
                assert (
                    await _fetch_table_persistence(migration_database_url, table_name)
                ) == "u"
        finally:
            await _drop_database(migration_database_url)
