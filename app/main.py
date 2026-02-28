import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text

from app.core.config import settings
from app.core.database import engine, Base
from app.models.models import Provider, HeaderBlocklistRule, UserSetting
from app.routers import (
    providers,
    models,
    endpoints,
    connections,
    proxy,
    stats,
    config,
    audit,
    settings as settings_router,
)

logger = logging.getLogger(__name__)

# Default providers to seed on first run
DEFAULT_PROVIDERS = [
    {
        "name": "OpenAI",
        "provider_type": "openai",
        "description": "OpenAI API (GPT models)",
    },
    {
        "name": "Anthropic",
        "provider_type": "anthropic",
        "description": "Anthropic API (Claude models)",
    },
    {
        "name": "Gemini",
        "provider_type": "gemini",
        "description": "Gemini API (Google)",
    },
]

SYSTEM_BLOCKLIST_DEFAULTS: list[dict] = [
    {"name": "Cloudflare headers", "match_type": "prefix", "pattern": "cf-"},
    {"name": "Cloudflare extended headers", "match_type": "prefix", "pattern": "x-cf-"},
    {
        "name": "Cloudflare Access headers",
        "match_type": "prefix",
        "pattern": "cf-access-",
    },
    {"name": "B3 tracing headers", "match_type": "prefix", "pattern": "x-b3-"},
    {
        "name": "Datadog tracing headers",
        "match_type": "prefix",
        "pattern": "x-datadog-",
    },
    {"name": "CDN loop detection", "match_type": "exact", "pattern": "cdn-loop"},
    {"name": "Forwarded header", "match_type": "exact", "pattern": "forwarded"},
    {"name": "Via header", "match_type": "exact", "pattern": "via"},
    {"name": "X-Forwarded-For", "match_type": "exact", "pattern": "x-forwarded-for"},
    {"name": "X-Forwarded-Host", "match_type": "exact", "pattern": "x-forwarded-host"},
    {"name": "X-Forwarded-Port", "match_type": "exact", "pattern": "x-forwarded-port"},
    {
        "name": "X-Forwarded-Proto",
        "match_type": "exact",
        "pattern": "x-forwarded-proto",
    },
    {"name": "X-Real-IP", "match_type": "exact", "pattern": "x-real-ip"},
    {"name": "True-Client-IP", "match_type": "exact", "pattern": "true-client-ip"},
    {"name": "W3C Traceparent", "match_type": "exact", "pattern": "traceparent"},
    {"name": "W3C Tracestate", "match_type": "exact", "pattern": "tracestate"},
    {"name": "W3C Baggage", "match_type": "exact", "pattern": "baggage"},
    {"name": "X-Request-ID", "match_type": "exact", "pattern": "x-request-id"},
    {"name": "X-Correlation-ID", "match_type": "exact", "pattern": "x-correlation-id"},
    {"name": "AWS X-Ray trace", "match_type": "exact", "pattern": "x-amzn-trace-id"},
    {
        "name": "GCP Cloud Trace",
        "match_type": "exact",
        "pattern": "x-cloud-trace-context",
    },
]


async def seed_providers():
    """Seed default providers if they don't exist."""
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Provider))
        existing = result.scalars().all()
        if not existing:
            for p in DEFAULT_PROVIDERS:
                session.add(Provider(**p))
            await session.commit()
            logger.info("Seeded default providers")


async def seed_header_blocklist_rules():
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        for d in SYSTEM_BLOCKLIST_DEFAULTS:
            existing = (
                await session.execute(
                    select(HeaderBlocklistRule).where(
                        HeaderBlocklistRule.match_type == d["match_type"],
                        HeaderBlocklistRule.pattern == d["pattern"],
                        HeaderBlocklistRule.is_system == True,  # noqa: E712
                    )
                )
            ).scalar_one_or_none()
            if not existing:
                session.add(
                    HeaderBlocklistRule(
                        name=d["name"],
                        match_type=d["match_type"],
                        pattern=d["pattern"],
                        enabled=True,
                        is_system=True,
                    )
                )
        await session.commit()
        logger.info("Seeded system header blocklist rules")


async def seed_user_settings():
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(UserSetting).order_by(UserSetting.id.asc()).limit(1)
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                UserSetting(report_currency_code="USD", report_currency_symbol="$")
            )
            await session.commit()
            logger.info("Seeded default user settings")


async def _table_exists(conn, table_name: str) -> bool:
    result = await conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :table_name"),
        {"table_name": table_name},
    )
    return result.first() is not None


async def _table_columns(conn, table_name: str) -> set[str]:
    if not await _table_exists(conn, table_name):
        return set()
    result = await conn.execute(text(f"PRAGMA table_info({table_name})"))
    return {row[1] for row in result.fetchall()}


async def _add_column_if_missing(
    conn,
    table_name: str,
    columns: set[str],
    column_name: str,
    ddl_fragment: str,
) -> None:
    if column_name in columns:
        return
    await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {ddl_fragment}"))
    columns.add(column_name)
    logger.info("Migrated: added %s column to %s table", column_name, table_name)


async def _rebuild_endpoint_fx_rate_settings_if_needed(conn) -> None:
    if not await _table_exists(conn, "endpoint_fx_rate_settings"):
        return

    fk_rows = (
        await conn.execute(text("PRAGMA foreign_key_list(endpoint_fx_rate_settings)"))
    ).fetchall()
    fk_targets = {row[2] for row in fk_rows}
    if fk_targets == {"endpoints"}:
        return

    await conn.execute(
        text(
            """
            CREATE TABLE endpoint_fx_rate_settings_tmp AS
            SELECT id, model_id, endpoint_id, fx_rate, created_at, updated_at
            FROM endpoint_fx_rate_settings
            """
        )
    )
    await conn.execute(text("DROP TABLE endpoint_fx_rate_settings"))
    await conn.execute(
        text(
            """
            CREATE TABLE endpoint_fx_rate_settings (
                id INTEGER PRIMARY KEY,
                model_id VARCHAR(200) NOT NULL,
                endpoint_id INTEGER NOT NULL,
                fx_rate VARCHAR(20) NOT NULL,
                created_at DATETIME,
                updated_at DATETIME,
                CONSTRAINT uq_fx_model_endpoint UNIQUE (model_id, endpoint_id),
                FOREIGN KEY(endpoint_id) REFERENCES endpoints(id) ON DELETE CASCADE
            )
            """
        )
    )
    await conn.execute(
        text(
            """
            INSERT INTO endpoint_fx_rate_settings (
                id,
                model_id,
                endpoint_id,
                fx_rate,
                created_at,
                updated_at
            )
            SELECT
                id,
                model_id,
                endpoint_id,
                fx_rate,
                created_at,
                updated_at
            FROM endpoint_fx_rate_settings_tmp
            WHERE endpoint_id IN (SELECT id FROM endpoints)
            """
        )
    )
    await conn.execute(text("DROP TABLE endpoint_fx_rate_settings_tmp"))
    logger.info("Migrated: rebuilt endpoint_fx_rate_settings foreign key to endpoints")


async def _migrate_legacy_endpoints_table(conn) -> None:
    endpoint_columns = await _table_columns(conn, "endpoints")
    if "model_config_id" not in endpoint_columns:
        return

    if await _table_exists(conn, "connections"):
        connection_columns = await _table_columns(conn, "connections")
        result = await conn.execute(text("SELECT COUNT(*) FROM connections"))
        connection_count = int(result.scalar_one() or 0)
        if connection_count > 0 and "base_url" not in connection_columns:
            logger.warning(
                "Detected pre-existing non-empty connections table; skipping legacy rename migration"
            )
            return
        await conn.execute(text("DROP TABLE connections"))

    await conn.execute(text("ALTER TABLE endpoints RENAME TO connections_legacy"))

    legacy_columns = await _table_columns(conn, "connections_legacy")
    await _add_column_if_missing(
        conn,
        "connections_legacy",
        legacy_columns,
        "health_status",
        "health_status VARCHAR(20) NOT NULL DEFAULT 'unknown'",
    )
    await _add_column_if_missing(
        conn,
        "connections_legacy",
        legacy_columns,
        "health_detail",
        "health_detail TEXT",
    )
    await _add_column_if_missing(
        conn,
        "connections_legacy",
        legacy_columns,
        "last_health_check",
        "last_health_check DATETIME",
    )
    await _add_column_if_missing(
        conn,
        "connections_legacy",
        legacy_columns,
        "pricing_enabled",
        "pricing_enabled BOOLEAN NOT NULL DEFAULT 0",
    )
    await _add_column_if_missing(
        conn,
        "connections_legacy",
        legacy_columns,
        "pricing_currency_code",
        "pricing_currency_code VARCHAR(3)",
    )
    await _add_column_if_missing(
        conn,
        "connections_legacy",
        legacy_columns,
        "input_price",
        "input_price VARCHAR(20)",
    )
    await _add_column_if_missing(
        conn,
        "connections_legacy",
        legacy_columns,
        "output_price",
        "output_price VARCHAR(20)",
    )
    await _add_column_if_missing(
        conn,
        "connections_legacy",
        legacy_columns,
        "cached_input_price",
        "cached_input_price VARCHAR(20)",
    )
    await _add_column_if_missing(
        conn,
        "connections_legacy",
        legacy_columns,
        "cache_creation_price",
        "cache_creation_price VARCHAR(20)",
    )
    await _add_column_if_missing(
        conn,
        "connections_legacy",
        legacy_columns,
        "reasoning_price",
        "reasoning_price VARCHAR(20)",
    )
    await _add_column_if_missing(
        conn,
        "connections_legacy",
        legacy_columns,
        "missing_special_token_price_policy",
        "missing_special_token_price_policy VARCHAR(20) NOT NULL DEFAULT 'MAP_TO_OUTPUT'",
    )
    await _add_column_if_missing(
        conn,
        "connections_legacy",
        legacy_columns,
        "pricing_config_version",
        "pricing_config_version INTEGER NOT NULL DEFAULT 0",
    )
    await _add_column_if_missing(
        conn,
        "connections_legacy",
        legacy_columns,
        "forward_stream_options",
        "forward_stream_options BOOLEAN NOT NULL DEFAULT 0",
    )

    await conn.execute(
        text(
            """
            CREATE TABLE endpoints (
                id INTEGER PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                base_url VARCHAR(500) NOT NULL,
                api_key VARCHAR(500) NOT NULL,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
    )
    await conn.execute(
        text(
            """
            INSERT INTO endpoints (id, name, base_url, api_key, created_at, updated_at)
            SELECT
                id,
                CASE
                    WHEN duplicate_count > 1 THEN base_name || '-' || id
                    ELSE base_name
                END,
                base_url,
                api_key,
                created_at,
                updated_at
            FROM (
                SELECT
                    c.id AS id,
                    c.base_url AS base_url,
                    c.api_key AS api_key,
                    c.created_at AS created_at,
                    c.updated_at AS updated_at,
                    CASE
                        WHEN TRIM(COALESCE(c.description, '')) <> '' THEN TRIM(c.description)
                        ELSE 'endpoint-' || c.id
                    END AS base_name,
                    COUNT(*) OVER (
                        PARTITION BY
                            CASE
                                WHEN TRIM(COALESCE(c.description, '')) <> '' THEN TRIM(c.description)
                                ELSE 'endpoint-' || c.id
                            END
                    ) AS duplicate_count
                FROM connections_legacy c
            ) AS named
            """
        )
    )
    await conn.execute(
        text("CREATE UNIQUE INDEX idx_endpoints_name_unique ON endpoints(name)")
    )

    await _rebuild_endpoint_fx_rate_settings_if_needed(conn)

    await conn.execute(
        text(
            """
            CREATE TABLE connections (
                id INTEGER PRIMARY KEY,
                model_config_id INTEGER NOT NULL,
                endpoint_id INTEGER NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                priority INTEGER NOT NULL DEFAULT 0,
                description TEXT,
                auth_type VARCHAR(50),
                custom_headers TEXT,
                health_status VARCHAR(20) NOT NULL DEFAULT 'unknown',
                health_detail TEXT,
                last_health_check DATETIME,
                pricing_enabled BOOLEAN NOT NULL DEFAULT 0,
                pricing_currency_code VARCHAR(3),
                input_price VARCHAR(20),
                output_price VARCHAR(20),
                cached_input_price VARCHAR(20),
                cache_creation_price VARCHAR(20),
                reasoning_price VARCHAR(20),
                missing_special_token_price_policy VARCHAR(20) NOT NULL DEFAULT 'MAP_TO_OUTPUT',
                pricing_config_version INTEGER NOT NULL DEFAULT 0,
                forward_stream_options BOOLEAN NOT NULL DEFAULT 0,
                created_at DATETIME,
                updated_at DATETIME,
                FOREIGN KEY(model_config_id) REFERENCES model_configs(id) ON DELETE CASCADE,
                FOREIGN KEY(endpoint_id) REFERENCES endpoints(id) ON DELETE RESTRICT
            )
            """
        )
    )
    await conn.execute(
        text(
            """
            INSERT INTO connections (
                id,
                model_config_id,
                endpoint_id,
                is_active,
                priority,
                description,
                auth_type,
                custom_headers,
                health_status,
                health_detail,
                last_health_check,
                pricing_enabled,
                pricing_currency_code,
                input_price,
                output_price,
                cached_input_price,
                cache_creation_price,
                reasoning_price,
                missing_special_token_price_policy,
                pricing_config_version,
                forward_stream_options,
                created_at,
                updated_at
            )
            SELECT
                id,
                model_config_id,
                id,
                COALESCE(is_active, 1),
                COALESCE(priority, 0),
                description,
                auth_type,
                custom_headers,
                COALESCE(health_status, 'unknown'),
                health_detail,
                last_health_check,
                COALESCE(pricing_enabled, 0),
                pricing_currency_code,
                input_price,
                output_price,
                cached_input_price,
                cache_creation_price,
                reasoning_price,
                COALESCE(missing_special_token_price_policy, 'MAP_TO_OUTPUT'),
                COALESCE(pricing_config_version, 0),
                COALESCE(forward_stream_options, 0),
                created_at,
                updated_at
            FROM connections_legacy
            """
        )
    )
    await conn.execute(text("DROP TABLE connections_legacy"))

    logger.info("Migrated: split legacy endpoints table into endpoints + connections")






async def _add_missing_columns(conn):
    await _migrate_legacy_endpoints_table(conn)

    endpoint_columns = await _table_columns(conn, "endpoints")
    if "name" not in endpoint_columns:
        await _add_column_if_missing(
            conn,
            "endpoints",
            endpoint_columns,
            "name",
            "name VARCHAR(200)",
        )
        await conn.execute(
            text(
                "UPDATE endpoints SET name = 'endpoint-' || id WHERE name IS NULL OR TRIM(name) = ''"
            )
        )
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_endpoints_name_unique ON endpoints(name)"
        )
    )

    connection_columns = await _table_columns(conn, "connections")
    if connection_columns:
        await _add_column_if_missing(
            conn,
            "connections",
            connection_columns,
            "endpoint_id",
            "endpoint_id INTEGER",
        )
        await conn.execute(
            text(
                "UPDATE connections SET endpoint_id = id WHERE endpoint_id IS NULL AND id IS NOT NULL"
            )
        )
        await _add_column_if_missing(
            conn,
            "connections",
            connection_columns,
            "health_status",
            "health_status VARCHAR(20) NOT NULL DEFAULT 'unknown'",
        )
        await _add_column_if_missing(
            conn,
            "connections",
            connection_columns,
            "health_detail",
            "health_detail TEXT",
        )
        await _add_column_if_missing(
            conn,
            "connections",
            connection_columns,
            "last_health_check",
            "last_health_check DATETIME",
        )
        await _add_column_if_missing(
            conn,
            "connections",
            connection_columns,
            "pricing_enabled",
            "pricing_enabled BOOLEAN NOT NULL DEFAULT 0",
        )
        await _add_column_if_missing(
            conn,
            "connections",
            connection_columns,
            "pricing_currency_code",
            "pricing_currency_code VARCHAR(3)",
        )
        await _add_column_if_missing(
            conn,
            "connections",
            connection_columns,
            "input_price",
            "input_price VARCHAR(20)",
        )
        await _add_column_if_missing(
            conn,
            "connections",
            connection_columns,
            "output_price",
            "output_price VARCHAR(20)",
        )
        await _add_column_if_missing(
            conn,
            "connections",
            connection_columns,
            "cached_input_price",
            "cached_input_price VARCHAR(20)",
        )
        await _add_column_if_missing(
            conn,
            "connections",
            connection_columns,
            "cache_creation_price",
            "cache_creation_price VARCHAR(20)",
        )
        await _add_column_if_missing(
            conn,
            "connections",
            connection_columns,
            "reasoning_price",
            "reasoning_price VARCHAR(20)",
        )
        await _add_column_if_missing(
            conn,
            "connections",
            connection_columns,
            "missing_special_token_price_policy",
            "missing_special_token_price_policy VARCHAR(20) NOT NULL DEFAULT 'MAP_TO_OUTPUT'",
        )
        await _add_column_if_missing(
            conn,
            "connections",
            connection_columns,
            "pricing_config_version",
            "pricing_config_version INTEGER NOT NULL DEFAULT 0",
        )
        await _add_column_if_missing(
            conn,
            "connections",
            connection_columns,
            "forward_stream_options",
            "forward_stream_options BOOLEAN NOT NULL DEFAULT 0",
        )


    provider_columns = await _table_columns(conn, "providers")
    if provider_columns:
        await _add_column_if_missing(
            conn,
            "providers",
            provider_columns,
            "audit_enabled",
            "audit_enabled BOOLEAN NOT NULL DEFAULT 0",
        )
        await _add_column_if_missing(
            conn,
            "providers",
            provider_columns,
            "audit_capture_bodies",
            "audit_capture_bodies BOOLEAN NOT NULL DEFAULT 1",
        )

    request_log_columns = await _table_columns(conn, "request_logs")
    if request_log_columns:
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "connection_id",
            "connection_id INTEGER",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "endpoint_description",
            "endpoint_description TEXT",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "success_flag",
            "success_flag BOOLEAN",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "billable_flag",
            "billable_flag BOOLEAN",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "priced_flag",
            "priced_flag BOOLEAN",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "unpriced_reason",
            "unpriced_reason VARCHAR(50)",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "reasoning_tokens",
            "reasoning_tokens INTEGER",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "input_cost_micros",
            "input_cost_micros BIGINT",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "output_cost_micros",
            "output_cost_micros BIGINT",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "reasoning_cost_micros",
            "reasoning_cost_micros BIGINT",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "total_cost_original_micros",
            "total_cost_original_micros BIGINT",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "total_cost_user_currency_micros",
            "total_cost_user_currency_micros BIGINT",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "currency_code_original",
            "currency_code_original VARCHAR(3)",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "report_currency_code",
            "report_currency_code VARCHAR(3)",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "report_currency_symbol",
            "report_currency_symbol VARCHAR(5)",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "fx_rate_used",
            "fx_rate_used VARCHAR(20)",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "fx_rate_source",
            "fx_rate_source VARCHAR(30)",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "pricing_snapshot_unit",
            "pricing_snapshot_unit VARCHAR(10)",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "pricing_snapshot_input",
            "pricing_snapshot_input VARCHAR(20)",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "pricing_snapshot_output",
            "pricing_snapshot_output VARCHAR(20)",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "pricing_snapshot_reasoning",
            "pricing_snapshot_reasoning VARCHAR(20)",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "cache_read_input_tokens",
            "cache_read_input_tokens INTEGER",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "cache_creation_input_tokens",
            "cache_creation_input_tokens INTEGER",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "cache_read_input_cost_micros",
            "cache_read_input_cost_micros BIGINT",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "cache_creation_input_cost_micros",
            "cache_creation_input_cost_micros BIGINT",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "pricing_snapshot_cache_read_input",
            "pricing_snapshot_cache_read_input VARCHAR(20)",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "pricing_snapshot_cache_creation_input",
            "pricing_snapshot_cache_creation_input VARCHAR(20)",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "pricing_snapshot_missing_special_token_price_policy",
            "pricing_snapshot_missing_special_token_price_policy VARCHAR(20)",
        )
        await _add_column_if_missing(
            conn,
            "request_logs",
            request_log_columns,
            "pricing_config_version_used",
            "pricing_config_version_used INTEGER",
        )

    user_settings_columns = await _table_columns(conn, "user_settings")
    if user_settings_columns:
        await _add_column_if_missing(
            conn,
            "user_settings",
            user_settings_columns,
            "timezone_preference",
            "timezone_preference VARCHAR(100)",
        )

    audit_columns = await _table_columns(conn, "audit_logs")
    if audit_columns:
        await _add_column_if_missing(
            conn,
            "audit_logs",
            audit_columns,
            "endpoint_id",
            "endpoint_id INTEGER",
        )
        await _add_column_if_missing(
            conn,
            "audit_logs",
            audit_columns,
            "connection_id",
            "connection_id INTEGER",
        )
        await _add_column_if_missing(
            conn,
            "audit_logs",
            audit_columns,
            "endpoint_base_url",
            "endpoint_base_url VARCHAR(500)",
        )
        await _add_column_if_missing(
            conn,
            "audit_logs",
            audit_columns,
            "endpoint_description",
            "endpoint_description TEXT",
        )

    await _rebuild_endpoint_fx_rate_settings_if_needed(conn)

    await conn.execute(
        text(
            "UPDATE request_logs SET connection_id = endpoint_id WHERE connection_id IS NULL AND endpoint_id IS NOT NULL"
        )
    )
    await conn.execute(
        text(
            """
            UPDATE request_logs
            SET endpoint_description = COALESCE(
                endpoint_description,
                (
                    SELECT c.description
                    FROM connections c
                    WHERE c.id = request_logs.connection_id
                ),
                (
                    SELECT e.name
                    FROM endpoints e
                    WHERE e.id = request_logs.endpoint_id
                )
            )
            WHERE request_logs.endpoint_description IS NULL
            """
        )
    )
    await conn.execute(
        text(
            """
            UPDATE audit_logs
            SET
                connection_id = COALESCE(
                    audit_logs.connection_id,
                    (
                        SELECT rl.connection_id
                        FROM request_logs rl
                        WHERE rl.id = audit_logs.request_log_id
                    ),
                    audit_logs.endpoint_id
                ),
                endpoint_id = COALESCE(
                    audit_logs.endpoint_id,
                    (
                        SELECT rl.endpoint_id
                        FROM request_logs rl
                        WHERE rl.id = audit_logs.request_log_id
                    )
                ),
                endpoint_base_url = COALESCE(
                    audit_logs.endpoint_base_url,
                    (
                        SELECT rl.endpoint_base_url
                        FROM request_logs rl
                        WHERE rl.id = audit_logs.request_log_id
                    )
                ),
                endpoint_description = COALESCE(
                    audit_logs.endpoint_description,
                    (
                        SELECT rl.endpoint_description
                        FROM request_logs rl
                        WHERE rl.id = audit_logs.request_log_id
                    )
                )
            WHERE audit_logs.request_log_id IS NOT NULL
            """
        )
    )

    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_connections_model_config_id ON connections(model_config_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_connections_endpoint_id ON connections(endpoint_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_connections_is_active ON connections(is_active)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_connections_priority ON connections(priority)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_audit_logs_endpoint_id ON audit_logs(endpoint_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_audit_logs_connection_id ON audit_logs(connection_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_request_logs_connection_id ON request_logs(connection_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_request_logs_billable_flag ON request_logs(billable_flag)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_request_logs_priced_flag ON request_logs(priced_flag)"
        )
    )
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_endpoint_fx_rate_settings_mapping ON endpoint_fx_rate_settings(model_id, endpoint_id)"
        )
    )

    model_config_columns = await _table_columns(conn, "model_configs")
    if model_config_columns:
        await _add_column_if_missing(
            conn,
            "model_configs",
            model_config_columns,
            "failover_recovery_enabled",
            "failover_recovery_enabled BOOLEAN NOT NULL DEFAULT 1",
        )
        await _add_column_if_missing(
            conn,
            "model_configs",
            model_config_columns,
            "failover_recovery_cooldown_seconds",
            "failover_recovery_cooldown_seconds INTEGER NOT NULL DEFAULT 60",
        )

    await conn.execute(
        text(
            "UPDATE model_configs SET lb_strategy = 'failover' WHERE lb_strategy = 'round_robin'"
        )
    )
    await conn.execute(
        text(
            """
            UPDATE request_logs
            SET success_flag = CASE
                WHEN status_code BETWEEN 200 AND 299 THEN 1
                ELSE 0
            END
            WHERE success_flag IS NULL
            """
        )
    )
    await conn.execute(
        text(
            """
            UPDATE request_logs
            SET billable_flag = CASE
                WHEN status_code BETWEEN 200 AND 299 THEN 1
                ELSE 0
            END
            WHERE billable_flag IS NULL
            """
        )
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create tables, seed data, init HTTP client
    async with engine.begin() as conn:
        await _add_missing_columns(conn)
        await conn.run_sync(Base.metadata.create_all)
        await _add_missing_columns(conn)

    await seed_providers()
    await seed_user_settings()
    await seed_header_blocklist_rules()

    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=settings.connect_timeout,
            read=settings.read_timeout,
            write=settings.write_timeout,
            pool=5.0,
        ),
        limits=httpx.Limits(max_connections=20),
        follow_redirects=True,
    )

    yield

    # Shutdown
    await app.state.http_client.aclose()
    await engine.dispose()


app = FastAPI(
    title="LLM Proxy Gateway",
    description="A lightweight proxy gateway for routing LLM API requests with load balancing and failover.",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# CORS — wildcard for local/LAN deployment
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

# Mount routers
app.include_router(providers.router)
app.include_router(models.router)
app.include_router(endpoints.router)
app.include_router(connections.router)
app.include_router(stats.router)
app.include_router(audit.router)
app.include_router(config.router)
app.include_router(settings_router.router)
app.include_router(proxy.router)


@app.get("/health", tags=["health"])
async def health_check():
    return {"status": "ok", "version": "0.1.0"}
