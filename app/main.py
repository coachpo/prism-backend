import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.core.config import ensure_postgresql_database_url, get_settings
from app.core.database import get_engine
from app.core.migrations import run_migrations
from app.core.auth import decode_access_token, extract_proxy_api_key
from app.core.crypto import encrypt_secret
from app.core.database import AsyncSessionLocal
from app.models.models import (
    AppAuthSettings,
    Endpoint,
    HeaderBlocklistRule,
    Profile,
    Provider,
    UserSetting,
)
from app.services.profile_invariants import ensure_profile_invariants
from app.services.auth_service import (
    get_or_create_app_auth_settings,
    verify_proxy_api_key,
)
from app.routers import (
    auth,
    profiles,
    providers,
    models,
    endpoints,
    connections,
    proxy,
    stats,
    config,
    audit,
    settings as settings_router,
    pricing_templates,
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


async def seed_profile_invariants():
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        await ensure_profile_invariants(session)
        await session.commit()
        logger.info("Ensured default profile invariants")


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
        profile_ids = (
            (
                await session.execute(
                    select(Profile.id)
                    .where(Profile.deleted_at.is_(None))
                    .order_by(Profile.id.asc())
                )
            )
            .scalars()
            .all()
        )
        if not profile_ids:
            return

        existing_profile_ids = set(
            (
                await session.execute(
                    select(UserSetting.profile_id).where(
                        UserSetting.profile_id.in_(profile_ids)
                    )
                )
            )
            .scalars()
            .all()
        )

        missing_profile_ids = [
            profile_id
            for profile_id in profile_ids
            if profile_id not in existing_profile_ids
        ]
        for profile_id in missing_profile_ids:
            session.add(
                UserSetting(
                    profile_id=profile_id,
                    report_currency_code="USD",
                    report_currency_symbol="$",
                    timezone_preference=None,
                )
            )

        if missing_profile_ids:
            await session.commit()
            logger.info(
                "Seeded default user settings for %d profile(s)",
                len(missing_profile_ids),
            )


async def seed_app_auth_settings() -> None:
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(AppAuthSettings)
                .where(AppAuthSettings.singleton_key == "app")
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(AppAuthSettings(singleton_key="app", auth_enabled=False))
            await session.commit()
            logger.info("Seeded application auth settings")


async def encrypt_endpoint_secrets() -> None:
    async with AsyncSessionLocal() as session:
        endpoints = (
            (await session.execute(select(Endpoint).order_by(Endpoint.id.asc())))
            .scalars()
            .all()
        )
        updated_count = 0
        for endpoint in endpoints:
            encrypted = encrypt_secret(endpoint.api_key)
            if encrypted != endpoint.api_key:
                endpoint.api_key = encrypted
                updated_count += 1
        if updated_count > 0:
            await session.commit()
            logger.info("Encrypted endpoint secrets for %d endpoint(s)", updated_count)


async def run_startup_migrations() -> None:
    settings = get_settings()
    ensure_postgresql_database_url(settings.database_url)
    await asyncio.to_thread(run_migrations, settings.database_url)
    logger.info("Applied database migrations")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: validate DB config, run migrations, seed data, init HTTP client
    await run_startup_migrations()
    await seed_providers()
    await seed_profile_invariants()
    await seed_user_settings()
    await seed_app_auth_settings()
    await encrypt_endpoint_secrets()
    await seed_header_blocklist_rules()

    settings = get_settings()
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
    await get_engine().dispose()


settings = get_settings()

app = FastAPI(
    title="LLM Proxy Gateway",
    description="A lightweight proxy gateway for routing LLM API requests with load balancing and failover.",
    version="0.1.0",
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[]
    if settings.cors_allows_any_origin
    else settings.cors_allowed_origins_list,
    allow_origin_regex=".*" if settings.cors_allows_any_origin else None,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


def build_auth_error_response(
    request, *, status_code: int, detail: str
) -> JSONResponse:
    response = JSONResponse(status_code=status_code, content={"detail": detail})
    origin = request.headers.get("origin")
    allowed_origins = settings.cors_allowed_origins_list
    if origin and (settings.cors_allows_any_origin or origin in allowed_origins):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"
    return response


@app.middleware("http")
async def authentication_middleware(request, call_next):
    if request.method.upper() == "OPTIONS":
        return await call_next(request)

    path = request.url.path
    if not (
        path.startswith("/api/")
        or path.startswith("/v1/")
        or path.startswith("/v1beta/")
    ):
        return await call_next(request)

    async with AsyncSessionLocal() as session:
        auth_settings = await get_or_create_app_auth_settings(session)
        request.state.auth_enabled = auth_settings.auth_enabled
        request.state.auth_subject = None
        request.state.proxy_api_key_id = None

        if path.startswith("/api/"):
            if not auth_settings.auth_enabled:
                return await call_next(request)
            public_paths = {
                "/api/auth/status",
                "/api/auth/login",
                "/api/auth/logout",
                "/api/auth/refresh",
                "/api/auth/password-reset/request",
                "/api/auth/password-reset/confirm",
            }
            if path in public_paths:
                return await call_next(request)

            token = request.cookies.get(settings.auth_cookie_name)
            if not token:
                return build_auth_error_response(
                    request, status_code=401, detail="Authentication required"
                )
            try:
                payload = decode_access_token(token)
            except Exception:
                return build_auth_error_response(
                    request, status_code=401, detail="Authentication required"
                )

            payload_subject = payload.get("sub")
            payload_token_version = payload.get("token_version")
            try:
                subject_id = int(str(payload_subject))
                token_version = int(str(payload_token_version))
            except (TypeError, ValueError):
                return build_auth_error_response(
                    request, status_code=401, detail="Authentication required"
                )

            if (
                subject_id != auth_settings.id
                or token_version != auth_settings.token_version
            ):
                return build_auth_error_response(
                    request, status_code=401, detail="Authentication required"
                )

            request.state.auth_subject = {
                "id": auth_settings.id,
                "username": auth_settings.username,
                "token_version": auth_settings.token_version,
            }
            return await call_next(request)

        if not auth_settings.auth_enabled:
            return await call_next(request)

        raw_key, _ = extract_proxy_api_key(
            {k.lower(): v for k, v in request.headers.items()}
        )
        if not raw_key:
            return build_auth_error_response(
                request, status_code=401, detail="Proxy API key required"
            )
        proxy_key = await verify_proxy_api_key(session, raw_key=raw_key)
        if proxy_key is None:
            return build_auth_error_response(
                request, status_code=401, detail="Invalid proxy API key"
            )
        proxy_key.last_used_ip = request.client.host if request.client else None
        request.state.proxy_api_key_id = proxy_key.id
        await session.commit()
    return await call_next(request)


# Mount routers
app.include_router(auth.router)
app.include_router(profiles.router)
app.include_router(providers.router)
app.include_router(models.router)
app.include_router(endpoints.router)
app.include_router(connections.router)
app.include_router(stats.router)
app.include_router(audit.router)
app.include_router(config.router)
app.include_router(settings_router.router)
app.include_router(pricing_templates.router)
app.include_router(proxy.router)


@app.get("/health", tags=["health"])
async def health_check():
    return {"status": "ok", "version": "0.1.0"}
