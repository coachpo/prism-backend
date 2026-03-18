from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from app import bootstrap
from app.core.config import Settings, get_settings
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


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default

    try:
        return int(value)
    except ValueError:
        return default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Prism backend server.")
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=_int_env("PORT", 8000))
    parser.add_argument(
        "--workers",
        type=int,
        default=_int_env("PRISM_BACKEND_WORKERS", 4),
    )
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "info"))
    parser.add_argument(
        "--forwarded-allow-ips",
        default=os.getenv("FORWARDED_ALLOW_IPS", "*"),
    )
    parser.add_argument(
        "--no-proxy-headers",
        action="store_false",
        dest="proxy_headers",
    )
    parser.set_defaults(proxy_headers=True)
    return parser


def _create_app(settings: Settings) -> FastAPI:
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
        return await bootstrap.handle_authentication(
            request,
            call_next,
            settings=settings,
        )

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

    return app


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    workers = 1 if args.reload else args.workers
    if workers > 1:
        from app.bootstrap.startup import (
            SKIP_STARTUP_SEQUENCE_ENV,
            run_startup_sequence,
        )

        asyncio.run(run_startup_sequence())
        os.environ[SKIP_STARTUP_SEQUENCE_ENV] = "1"

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        workers=workers,
        reload=args.reload,
        log_level=args.log_level,
        proxy_headers=args.proxy_headers,
        forwarded_allow_ips=args.forwarded_allow_ips,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.background_task_manager = None
    app.state.http_client = None
    http_client = None

    try:
        await bootstrap.run_startup_sequence()
        http_client = bootstrap.build_http_client()
        await background_task_manager.start()
    except Exception:
        if http_client is not None:
            await http_client.aclose()
        await get_engine().dispose()
        raise

    app.state.http_client = http_client
    app.state.background_task_manager = background_task_manager

    try:
        yield
    finally:
        try:
            await background_task_manager.shutdown()
        finally:
            app.state.background_task_manager = None
            try:
                if http_client is not None:
                    await http_client.aclose()
            finally:
                app.state.http_client = None
                await get_engine().dispose()


app = _create_app(get_settings())


if __name__ == "__main__":
    main()
