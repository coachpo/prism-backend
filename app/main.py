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


async def _add_missing_columns(conn):
    """Add columns introduced after initial schema to existing SQLite tables."""
    result = await conn.execute(text("PRAGMA table_info(endpoints)"))
    ep_columns = {row[1] for row in result.fetchall()}
    if "auth_type" not in ep_columns:
        await conn.execute(
            text("ALTER TABLE endpoints ADD COLUMN auth_type VARCHAR(50)")
        )
        logger.info("Migrated: added auth_type column to endpoints table")
    if "custom_headers" not in ep_columns:
        await conn.execute(text("ALTER TABLE endpoints ADD COLUMN custom_headers TEXT"))
        logger.info("Migrated: added custom_headers column to endpoints table")
    if "pricing_enabled" not in ep_columns:
        await conn.execute(
            text(
                "ALTER TABLE endpoints ADD COLUMN pricing_enabled BOOLEAN NOT NULL DEFAULT 0"
            )
        )
        logger.info("Migrated: added pricing_enabled column to endpoints table")
    if "pricing_unit" not in ep_columns:
        await conn.execute(
            text("ALTER TABLE endpoints ADD COLUMN pricing_unit VARCHAR(10)")
        )
        logger.info("Migrated: added pricing_unit column to endpoints table")
    if "pricing_currency_code" not in ep_columns:
        await conn.execute(
            text("ALTER TABLE endpoints ADD COLUMN pricing_currency_code VARCHAR(3)")
        )
        logger.info("Migrated: added pricing_currency_code column to endpoints table")
    if "input_price" not in ep_columns:
        await conn.execute(
            text("ALTER TABLE endpoints ADD COLUMN input_price VARCHAR(20)")
        )
        logger.info("Migrated: added input_price column to endpoints table")
    if "output_price" not in ep_columns:
        await conn.execute(
            text("ALTER TABLE endpoints ADD COLUMN output_price VARCHAR(20)")
        )
        logger.info("Migrated: added output_price column to endpoints table")
    if "cached_input_price" not in ep_columns:
        await conn.execute(
            text("ALTER TABLE endpoints ADD COLUMN cached_input_price VARCHAR(20)")
        )
        logger.info("Migrated: added cached_input_price column to endpoints table")
    if "cache_creation_price" not in ep_columns:
        await conn.execute(
            text("ALTER TABLE endpoints ADD COLUMN cache_creation_price VARCHAR(20)")
        )
        logger.info("Migrated: added cache_creation_price column to endpoints table")
    if "reasoning_price" not in ep_columns:
        await conn.execute(
            text("ALTER TABLE endpoints ADD COLUMN reasoning_price VARCHAR(20)")
        )
        logger.info("Migrated: added reasoning_price column to endpoints table")
    if "missing_special_token_price_policy" not in ep_columns:
        await conn.execute(
            text(
                "ALTER TABLE endpoints ADD COLUMN missing_special_token_price_policy VARCHAR(20) NOT NULL DEFAULT 'MAP_TO_OUTPUT'"
            )
        )
        logger.info(
            "Migrated: added missing_special_token_price_policy column to endpoints table"
        )
    if "pricing_config_version" not in ep_columns:
        await conn.execute(
            text(
                "ALTER TABLE endpoints ADD COLUMN pricing_config_version INTEGER NOT NULL DEFAULT 0"
            )
        )
        logger.info("Migrated: added pricing_config_version column to endpoints table")

    result = await conn.execute(text("PRAGMA table_info(providers)"))
    prov_columns = {row[1] for row in result.fetchall()}
    if "audit_enabled" not in prov_columns:
        await conn.execute(
            text(
                "ALTER TABLE providers ADD COLUMN audit_enabled BOOLEAN NOT NULL DEFAULT 0"
            )
        )
        logger.info("Migrated: added audit_enabled column to providers table")
    if "audit_capture_bodies" not in prov_columns:
        await conn.execute(
            text(
                "ALTER TABLE providers ADD COLUMN audit_capture_bodies BOOLEAN NOT NULL DEFAULT 1"
            )
        )
        logger.info("Migrated: added audit_capture_bodies column to providers table")

    result = await conn.execute(text("PRAGMA table_info(request_logs)"))
    rl_columns = {row[1] for row in result.fetchall()}
    if "endpoint_description" not in rl_columns:
        await conn.execute(
            text("ALTER TABLE request_logs ADD COLUMN endpoint_description TEXT")
        )
        logger.info("Migrated: added endpoint_description column to request_logs table")
    if "success_flag" not in rl_columns:
        await conn.execute(
            text("ALTER TABLE request_logs ADD COLUMN success_flag BOOLEAN")
        )
        logger.info("Migrated: added success_flag column to request_logs table")
    if "billable_flag" not in rl_columns:
        await conn.execute(
            text("ALTER TABLE request_logs ADD COLUMN billable_flag BOOLEAN")
        )
        logger.info("Migrated: added billable_flag column to request_logs table")
    if "priced_flag" not in rl_columns:
        await conn.execute(
            text("ALTER TABLE request_logs ADD COLUMN priced_flag BOOLEAN")
        )
        logger.info("Migrated: added priced_flag column to request_logs table")
    if "unpriced_reason" not in rl_columns:
        await conn.execute(
            text("ALTER TABLE request_logs ADD COLUMN unpriced_reason VARCHAR(50)")
        )
        logger.info("Migrated: added unpriced_reason column to request_logs table")
    if "reasoning_tokens" not in rl_columns:
        await conn.execute(
            text("ALTER TABLE request_logs ADD COLUMN reasoning_tokens INTEGER")
        )
        logger.info("Migrated: added reasoning_tokens column to request_logs table")
    if "input_cost_micros" not in rl_columns:
        await conn.execute(
            text("ALTER TABLE request_logs ADD COLUMN input_cost_micros BIGINT")
        )
        logger.info("Migrated: added input_cost_micros column to request_logs table")
    if "output_cost_micros" not in rl_columns:
        await conn.execute(
            text("ALTER TABLE request_logs ADD COLUMN output_cost_micros BIGINT")
        )
        logger.info("Migrated: added output_cost_micros column to request_logs table")
    if "reasoning_cost_micros" not in rl_columns:
        await conn.execute(
            text("ALTER TABLE request_logs ADD COLUMN reasoning_cost_micros BIGINT")
        )
        logger.info(
            "Migrated: added reasoning_cost_micros column to request_logs table"
        )
    if "total_cost_original_micros" not in rl_columns:
        await conn.execute(
            text(
                "ALTER TABLE request_logs ADD COLUMN total_cost_original_micros BIGINT"
            )
        )
        logger.info(
            "Migrated: added total_cost_original_micros column to request_logs table"
        )
    if "total_cost_user_currency_micros" not in rl_columns:
        await conn.execute(
            text(
                "ALTER TABLE request_logs ADD COLUMN total_cost_user_currency_micros BIGINT"
            )
        )
        logger.info(
            "Migrated: added total_cost_user_currency_micros column to request_logs table"
        )
    if "currency_code_original" not in rl_columns:
        await conn.execute(
            text(
                "ALTER TABLE request_logs ADD COLUMN currency_code_original VARCHAR(3)"
            )
        )
        logger.info(
            "Migrated: added currency_code_original column to request_logs table"
        )
    if "report_currency_code" not in rl_columns:
        await conn.execute(
            text("ALTER TABLE request_logs ADD COLUMN report_currency_code VARCHAR(3)")
        )
        logger.info("Migrated: added report_currency_code column to request_logs table")
    if "report_currency_symbol" not in rl_columns:
        await conn.execute(
            text(
                "ALTER TABLE request_logs ADD COLUMN report_currency_symbol VARCHAR(5)"
            )
        )
        logger.info(
            "Migrated: added report_currency_symbol column to request_logs table"
        )
    if "fx_rate_used" not in rl_columns:
        await conn.execute(
            text("ALTER TABLE request_logs ADD COLUMN fx_rate_used VARCHAR(20)")
        )
        logger.info("Migrated: added fx_rate_used column to request_logs table")
    if "fx_rate_source" not in rl_columns:
        await conn.execute(
            text("ALTER TABLE request_logs ADD COLUMN fx_rate_source VARCHAR(30)")
        )
        logger.info("Migrated: added fx_rate_source column to request_logs table")
    if "pricing_snapshot_unit" not in rl_columns:
        await conn.execute(
            text(
                "ALTER TABLE request_logs ADD COLUMN pricing_snapshot_unit VARCHAR(10)"
            )
        )
        logger.info(
            "Migrated: added pricing_snapshot_unit column to request_logs table"
        )
    if "pricing_snapshot_input" not in rl_columns:
        await conn.execute(
            text(
                "ALTER TABLE request_logs ADD COLUMN pricing_snapshot_input VARCHAR(20)"
            )
        )
        logger.info(
            "Migrated: added pricing_snapshot_input column to request_logs table"
        )
    if "pricing_snapshot_output" not in rl_columns:
        await conn.execute(
            text(
                "ALTER TABLE request_logs ADD COLUMN pricing_snapshot_output VARCHAR(20)"
            )
        )
        logger.info(
            "Migrated: added pricing_snapshot_output column to request_logs table"
        )
    if "pricing_snapshot_reasoning" not in rl_columns:
        await conn.execute(
            text(
                "ALTER TABLE request_logs ADD COLUMN pricing_snapshot_reasoning VARCHAR(20)"
            )
        )
        logger.info(
            "Migrated: added pricing_snapshot_reasoning column to request_logs table"
        )
    if "cache_read_input_tokens" not in rl_columns:
        await conn.execute(
            text("ALTER TABLE request_logs ADD COLUMN cache_read_input_tokens INTEGER")
        )
        logger.info(
            "Migrated: added cache_read_input_tokens column to request_logs table"
        )
    if "cache_creation_input_tokens" not in rl_columns:
        await conn.execute(
            text(
                "ALTER TABLE request_logs ADD COLUMN cache_creation_input_tokens INTEGER"
            )
        )
        logger.info(
            "Migrated: added cache_creation_input_tokens column to request_logs table"
        )
    if "cache_read_input_cost_micros" not in rl_columns:
        await conn.execute(
            text(
                "ALTER TABLE request_logs ADD COLUMN cache_read_input_cost_micros BIGINT"
            )
        )
        logger.info(
            "Migrated: added cache_read_input_cost_micros column to request_logs table"
        )
    if "cache_creation_input_cost_micros" not in rl_columns:
        await conn.execute(
            text(
                "ALTER TABLE request_logs ADD COLUMN cache_creation_input_cost_micros BIGINT"
            )
        )
        logger.info(
            "Migrated: added cache_creation_input_cost_micros column to request_logs table"
        )
    if "pricing_snapshot_cache_read_input" not in rl_columns:
        await conn.execute(
            text(
                "ALTER TABLE request_logs ADD COLUMN pricing_snapshot_cache_read_input VARCHAR(20)"
            )
        )
        logger.info(
            "Migrated: added pricing_snapshot_cache_read_input column to request_logs table"
        )
    if "pricing_snapshot_cache_creation_input" not in rl_columns:
        await conn.execute(
            text(
                "ALTER TABLE request_logs ADD COLUMN pricing_snapshot_cache_creation_input VARCHAR(20)"
            )
        )
        logger.info(
            "Migrated: added pricing_snapshot_cache_creation_input column to request_logs table"
        )
    if "pricing_snapshot_missing_special_token_price_policy" not in rl_columns:
        await conn.execute(
            text(
                "ALTER TABLE request_logs ADD COLUMN pricing_snapshot_missing_special_token_price_policy VARCHAR(20)"
            )
        )
        logger.info(
            "Migrated: added pricing_snapshot_missing_special_token_price_policy column to request_logs table"
        )
    if "pricing_config_version_used" not in rl_columns:
        await conn.execute(
            text(
                "ALTER TABLE request_logs ADD COLUMN pricing_config_version_used INTEGER"
            )
        )
        logger.info(
            "Migrated: added pricing_config_version_used column to request_logs table"
        )

    result = await conn.execute(text("PRAGMA table_info(audit_logs)"))
    al_columns = {row[1] for row in result.fetchall()}
    if "endpoint_id" not in al_columns:
        await conn.execute(
            text("ALTER TABLE audit_logs ADD COLUMN endpoint_id INTEGER")
        )
        logger.info("Migrated: added endpoint_id column to audit_logs table")
    if "endpoint_base_url" not in al_columns:
        await conn.execute(
            text("ALTER TABLE audit_logs ADD COLUMN endpoint_base_url VARCHAR(500)")
        )
        logger.info("Migrated: added endpoint_base_url column to audit_logs table")
    if "endpoint_description" not in al_columns:
        await conn.execute(
            text("ALTER TABLE audit_logs ADD COLUMN endpoint_description TEXT")
        )
        logger.info("Migrated: added endpoint_description column to audit_logs table")

    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_audit_logs_endpoint_id ON audit_logs(endpoint_id)"
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

    await conn.execute(
        text(
            """
            UPDATE request_logs SET endpoint_description = (
                SELECT endpoints.description FROM endpoints
                WHERE endpoints.id = request_logs.endpoint_id
            )
            WHERE request_logs.endpoint_id IS NOT NULL
              AND request_logs.endpoint_description IS NULL
            """
        )
    )

    await conn.execute(
        text(
            """
            UPDATE audit_logs SET
                endpoint_id = rl.endpoint_id,
                endpoint_base_url = rl.endpoint_base_url,
                endpoint_description = rl.endpoint_description
            FROM request_logs rl
            WHERE audit_logs.request_log_id = rl.id
              AND audit_logs.request_log_id IS NOT NULL
              AND audit_logs.endpoint_id IS NULL
            """
        )
    )

    # --- model_configs: add failover recovery columns + migrate round_robin ---
    result = await conn.execute(text("PRAGMA table_info(model_configs)"))
    mc_columns = {row[1] for row in result.fetchall()}
    if "failover_recovery_enabled" not in mc_columns:
        await conn.execute(
            text(
                "ALTER TABLE model_configs ADD COLUMN failover_recovery_enabled BOOLEAN NOT NULL DEFAULT 1"
            )
        )
        logger.info(
            "Migrated: added failover_recovery_enabled column to model_configs table"
        )
    if "failover_recovery_cooldown_seconds" not in mc_columns:
        await conn.execute(
            text(
                "ALTER TABLE model_configs ADD COLUMN failover_recovery_cooldown_seconds INTEGER NOT NULL DEFAULT 60"
            )
        )
        logger.info(
            "Migrated: added failover_recovery_cooldown_seconds column to model_configs table"
        )
    # Migrate any leftover round_robin strategies to failover
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
app.include_router(stats.router)
app.include_router(audit.router)
app.include_router(config.router)
app.include_router(settings_router.router)
app.include_router(proxy.router)


@app.get("/health", tags=["health"])
async def health_check():
    return {"status": "ok", "version": "0.1.0"}
