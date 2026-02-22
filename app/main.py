import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text

from app.core.config import settings
from app.core.database import engine, Base
from app.models.models import Provider, HeaderBlocklistRule
from app.routers import providers, models, endpoints, proxy, stats, config, audit

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create tables, seed data, init HTTP client
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _add_missing_columns(conn)

    await seed_providers()
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
app.include_router(proxy.router)


@app.get("/health", tags=["health"])
async def health_check():
    return {"status": "ok", "version": "0.1.0"}
