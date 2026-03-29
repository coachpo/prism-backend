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


class TestDEF081_ProxyTargetSchemaContract:
    def test_model_contract_uses_proxy_targets_and_resolved_target_logging(self):
        from app.schemas.schemas import (
            ModelConfigCreate,
            ModelConfigListResponse,
            ModelConfigResponse,
            ModelConfigUpdate,
            RequestLogResponse,
        )

        for schema in (
            ModelConfigCreate,
            ModelConfigUpdate,
            ModelConfigResponse,
            ModelConfigListResponse,
        ):
            fields = set(schema.model_fields.keys())
            assert "proxy_targets" in fields

        assert "resolved_target_model_id" in RequestLogResponse.model_fields

    @pytest.mark.asyncio
    async def test_initial_schema_creates_proxy_target_table_and_auto_recovery_storage(
        self, test_database_url: str
    ):
        migration_database_url = _database_url_with_name(
            test_database_url, f"prism_def081_{uuid4().hex[:12]}"
        )
        expected_head_revision = _get_current_head_revision(migration_database_url)

        assert expected_head_revision != "0001_initial"

        await _create_database(migration_database_url)
        try:
            await asyncio.to_thread(_upgrade_database, migration_database_url, "head")

            engine = create_async_engine(migration_database_url)
            async with engine.connect() as conn:
                assert await _fetch_current_revision(migration_database_url) == [
                    expected_head_revision
                ]

                proxy_targets_table = (
                    await conn.execute(
                        text("SELECT to_regclass('public.model_proxy_targets')")
                    )
                ).scalar_one()
                resolved_target_column = (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'request_logs' AND column_name = 'resolved_target_model_id'"
                        )
                    )
                ).scalar_one_or_none()
                routing_policy_column = (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'loadbalance_strategies' AND column_name = 'routing_policy'"
                        )
                    )
                ).scalar_one_or_none()
                removed_recovery_column = (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'loadbalance_strategies' AND column_name = 'auto_recovery'"
                        )
                    )
                ).scalar_one_or_none()

                assert proxy_targets_table == "model_proxy_targets"
                assert resolved_target_column == "resolved_target_model_id"
                assert routing_policy_column == "routing_policy"
                assert removed_recovery_column is None
            await engine.dispose()
        finally:
            await _drop_database(migration_database_url)
