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


async def _seed_drifted_pre_0008_schema(database_url: str) -> None:
    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        await conn.execute(
            text(
                "INSERT INTO alembic_version (version_num) VALUES ('0007_refresh_session_duration')"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE providers (id INTEGER, name VARCHAR(100), provider_type VARCHAR(50), description TEXT, audit_enabled BOOLEAN, audit_capture_bodies BOOLEAN, created_at TIMESTAMP WITH TIME ZONE, updated_at TIMESTAMP WITH TIME ZONE)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO providers (id, name, provider_type, description, audit_enabled, audit_capture_bodies, created_at, updated_at) VALUES (1, 'openai', 'openai', NULL, false, true, NOW(), NOW())"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE profiles (id INTEGER, name VARCHAR(200), description TEXT, is_active BOOLEAN, version INTEGER, is_default BOOLEAN, is_editable BOOLEAN, deleted_at TIMESTAMP WITH TIME ZONE, created_at TIMESTAMP WITH TIME ZONE, updated_at TIMESTAMP WITH TIME ZONE)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO profiles (id, name, description, is_active, version, is_default, is_editable, deleted_at, created_at, updated_at) VALUES (1, 'Default', 'System default profile', true, 1, true, true, NULL, NOW(), NOW())"
            )
        )
    await engine.dispose()


class TestDEF075_LoadbalanceMigrationRepairsLegacyOwnerPrimaryKeys:
    @pytest.mark.asyncio
    async def test_upgrade_repairs_profiles_and_providers_before_creating_loadbalance_events(
        self, test_database_url: str
    ):
        drift_database_url = _database_url_with_name(
            test_database_url, f"prism_def075_{uuid4().hex[:12]}"
        )

        await _create_database(drift_database_url)
        try:
            await _seed_drifted_pre_0008_schema(drift_database_url)
            await asyncio.to_thread(
                _upgrade_database, drift_database_url, "0008_loadbalance_events"
            )

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
                providers_pk = (
                    (
                        await conn.execute(
                            text(
                                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'providers'::regclass AND contype = 'p'"
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
            await engine.dispose()

            assert version == ["0008_loadbalance_events"]
            assert profiles_pk == ["PRIMARY KEY (id)"]
            assert providers_pk == ["PRIMARY KEY (id)"]
            assert loadbalance_table == "loadbalance_events"
        finally:
            await _drop_database(drift_database_url)
