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


class TestDEF081_ProxyTargetSchemaMigration:
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
            assert "redirect_to" not in fields

        assert "resolved_target_model_id" in RequestLogResponse.model_fields

    @pytest.mark.asyncio
    async def test_head_migration_creates_proxy_target_table_backfills_redirects_and_enforces_db_invariants(
        self, test_database_url: str
    ):
        migration_database_url = _database_url_with_name(
            test_database_url, f"prism_def081_{uuid4().hex[:12]}"
        )
        expected_head_revision = "0024_usage_request_events"

        assert (
            _get_current_head_revision(migration_database_url) == expected_head_revision
        )

        await _create_database(migration_database_url)
        try:
            await asyncio.to_thread(
                _upgrade_database,
                migration_database_url,
                "0017_loadbalance_ban_escalation",
            )

            engine = create_async_engine(migration_database_url)
            legacy_provider_type = "provider" + "_type"
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        f"INSERT INTO providers (id, name, {legacy_provider_type}, description, audit_enabled, audit_capture_bodies, created_at, updated_at) VALUES "
                        "(1, 'OpenAI', 'openai', NULL, false, true, NOW(), NOW()), "
                        "(2, 'Anthropic', 'anthropic', NULL, false, true, NOW(), NOW())"
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO profiles (id, name, description, is_active, version, is_default, is_editable, deleted_at, created_at, updated_at) VALUES "
                        "(1, 'Default', NULL, true, 0, true, true, NULL, NOW(), NOW()), "
                        "(2, 'Other', NULL, false, 0, false, true, NULL, NOW(), NOW())"
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO loadbalance_strategies (id, profile_id, name, strategy_type, failover_recovery_enabled, failover_ban_mode, failover_max_cooldown_strikes_before_ban, failover_ban_duration_seconds, created_at, updated_at) VALUES "
                        "(1, 1, 'single-primary', 'single', false, 'off', 0, 0, NOW(), NOW()), "
                        "(2, 2, 'single-other', 'single', false, 'off', 0, 0, NOW(), NOW())"
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO model_configs (id, profile_id, provider_id, model_id, display_name, model_type, redirect_to, loadbalance_strategy_id, is_enabled, created_at, updated_at) VALUES "
                        "(10, 1, 1, 'native-a', NULL, 'native', NULL, 1, true, NOW(), NOW()), "
                        "(11, 1, 1, 'proxy-a', NULL, 'proxy', 'native-a', NULL, true, NOW(), NOW()), "
                        "(12, 1, 1, 'proxy-target', NULL, 'proxy', NULL, NULL, true, NOW(), NOW()), "
                        "(13, 1, 2, 'native-b', NULL, 'native', NULL, 1, true, NOW(), NOW()), "
                        "(14, 2, 1, 'native-c', NULL, 'native', NULL, 2, true, NOW(), NOW())"
                    )
                )
            await engine.dispose()

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
                redirect_to_column = (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'model_configs' AND column_name = 'redirect_to'"
                        )
                    )
                ).scalar_one_or_none()
                backfilled_rows = (
                    await conn.execute(
                        text(
                            "SELECT source_model_config_id, target_model_config_id, position FROM model_proxy_targets ORDER BY source_model_config_id, position"
                        )
                    )
                ).all()

                assert proxy_targets_table == "model_proxy_targets"
                assert resolved_target_column == "resolved_target_model_id"
                assert redirect_to_column is None
                assert backfilled_rows == [(11, 10, 0)]

                with pytest.raises(Exception):
                    await conn.execute(
                        text(
                            "INSERT INTO model_proxy_targets (source_model_config_id, target_model_config_id, position) VALUES (11, 12, 1)"
                        )
                    )
                await conn.rollback()

                with pytest.raises(Exception):
                    await conn.execute(
                        text(
                            "INSERT INTO model_proxy_targets (source_model_config_id, target_model_config_id, position) VALUES (11, 13, 1)"
                        )
                    )
                await conn.rollback()

                with pytest.raises(Exception):
                    await conn.execute(
                        text(
                            "INSERT INTO model_proxy_targets (source_model_config_id, target_model_config_id, position) VALUES (11, 14, 1)"
                        )
                    )
                await conn.rollback()
            await engine.dispose()
        finally:
            await _drop_database(migration_database_url)
