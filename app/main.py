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
from app.core.version import get_backend_version
from app.services.background_tasks import background_task_manager
from app.services.loadbalancer.limiter import reconcile_all_connection_limits
from app.services.monitoring_service import MonitoringScheduler
from app.services.stats.logging import shutdown_dashboard_update_lifecycle
from app.routers import (
    audit,
    auth,
    config,
    connections,
    endpoints,
    loadbalance,
    models,
    monitoring,
    pricing_templates,
    profiles,
    proxy,
    realtime,
    settings as settings_router,
    stats,
    vendors,
)

DEFAULT_ADAPTIVE_LOADBALANCE_STRATEGY_PRESET_NAME = (
    bootstrap.DEFAULT_ADAPTIVE_LOADBALANCE_STRATEGY_PRESET_NAME
)
DEFAULT_LEGACY_LOADBALANCE_STRATEGY_PRESET_NAME = (
    bootstrap.DEFAULT_LEGACY_LOADBALANCE_STRATEGY_PRESET_NAME
)
DEFAULT_VENDORS = bootstrap.DEFAULT_VENDORS
DEFAULT_LOADBALANCE_STRATEGY_PRESET_NAME = (
    bootstrap.DEFAULT_LOADBALANCE_STRATEGY_PRESET_NAME
)
SYSTEM_BLOCKLIST_DEFAULTS = bootstrap.SYSTEM_BLOCKLIST_DEFAULTS
build_auth_error_response = bootstrap.build_auth_error_response
encrypt_endpoint_secrets = bootstrap.encrypt_endpoint_secrets
run_startup_migrations = bootstrap.run_startup_migrations
seed_app_auth_settings = bootstrap.seed_app_auth_settings
seed_header_blocklist_rules = bootstrap.seed_header_blocklist_rules
seed_loadbalance_strategy_presets = bootstrap.seed_loadbalance_strategy_presets
seed_profile_invariants = bootstrap.seed_profile_invariants
seed_vendors = bootstrap.seed_vendors
seed_user_settings = bootstrap.seed_user_settings

APP_VERSION = get_backend_version()


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
        version=APP_VERSION,
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
        vendors.router,
        models.router,
        endpoints.router,
        connections.router,
        stats.router,
        monitoring.router,
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
        return {"status": "ok", "version": APP_VERSION}

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
    app.state.monitoring_scheduler = None
    http_client = None
    monitoring_scheduler = None
    settings = get_settings()

    try:
        await bootstrap.run_startup_sequence()
        await reconcile_all_connection_limits()
        http_client = bootstrap.build_http_client()
        background_task_manager.configure(
            worker_count=settings.background_task_worker_count
        )
        await background_task_manager.start()
        monitoring_scheduler = MonitoringScheduler(http_client=http_client)
        await monitoring_scheduler.start()
    except Exception:
        if http_client is not None:
            await http_client.aclose()
        await get_engine().dispose()
        raise

    app.state.http_client = http_client
    app.state.background_task_manager = background_task_manager
    app.state.monitoring_scheduler = monitoring_scheduler

    try:
        yield
    finally:
        try:
            if monitoring_scheduler is not None:
                await monitoring_scheduler.stop()
            await shutdown_dashboard_update_lifecycle()
            await background_task_manager.shutdown()
        finally:
            app.state.background_task_manager = None
            app.state.monitoring_scheduler = None
            try:
                if http_client is not None:
                    await http_client.aclose()
            finally:
                app.state.http_client = None
                await get_engine().dispose()


app = _create_app(get_settings())


if __name__ == "__main__":
    main()
