import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.core.config import settings, ensure_postgresql_database_url
from app.core.database import engine
from app.core.migrations import run_migrations
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


async def run_startup_migrations() -> None:
    ensure_postgresql_database_url(settings.database_url)
    await asyncio.to_thread(run_migrations, settings.database_url)
    logger.info("Applied database migrations")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: validate DB config, run migrations, seed data, init HTTP client
    await run_startup_migrations()
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
