import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.core.config import settings
from app.core.database import engine, Base
from app.models.models import Provider
from app.routers import providers, models, endpoints, proxy

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
        "name": "Google Gemini",
        "provider_type": "gemini",
        "description": "Google Gemini API",
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create tables, seed data, init HTTP client
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await seed_providers()

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
app.include_router(proxy.router)


@app.get("/health", tags=["health"])
async def health_check():
    return {"status": "ok", "version": "0.1.0"}
