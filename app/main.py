from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import bootstrap
from app.core.config import get_settings
from app.core.database import get_engine
from app.services.background_tasks import background_task_manager
from app.routers import (
    audit,
    auth,
    config,
    connections,
    endpoints,
    loadbalance,
    models,
    pricing_templates,
    profiles,
    providers,
    proxy,
    realtime,
    settings as settings_router,
    stats,
)

DEFAULT_PROVIDERS = bootstrap.DEFAULT_PROVIDERS
SYSTEM_BLOCKLIST_DEFAULTS = bootstrap.SYSTEM_BLOCKLIST_DEFAULTS
build_auth_error_response = bootstrap.build_auth_error_response
encrypt_endpoint_secrets = bootstrap.encrypt_endpoint_secrets
run_startup_migrations = bootstrap.run_startup_migrations
seed_app_auth_settings = bootstrap.seed_app_auth_settings
seed_header_blocklist_rules = bootstrap.seed_header_blocklist_rules
seed_profile_invariants = bootstrap.seed_profile_invariants
seed_providers = bootstrap.seed_providers
seed_user_settings = bootstrap.seed_user_settings

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bootstrap.run_startup_sequence()
    app.state.http_client = bootstrap.build_http_client()
    app.state.background_task_manager = background_task_manager
    await background_task_manager.start()
    yield
    await background_task_manager.shutdown()
    await app.state.http_client.aclose()
    await get_engine().dispose()


app = FastAPI(
    title="LLM Proxy Gateway",
    description=(
        "A lightweight proxy gateway for routing LLM API requests with load "
        "balancing and failover."
    ),
    version="0.1.0",
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins_list or ["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.middleware("http")
async def authentication_middleware(request, call_next):
    return await bootstrap.handle_authentication(request, call_next, settings=settings)


for router in (
    auth.router,
    profiles.router,
    providers.router,
    models.router,
    endpoints.router,
    connections.router,
    stats.router,
    audit.router,
    loadbalance.router,
    config.router,
    settings_router.router,
    pricing_templates.router,
    realtime.router,
    proxy.router,
):
    app.include_router(router)


@app.get("/health", tags=["health"])
async def health_check():
    return {"status": "ok", "version": "0.1.0"}
