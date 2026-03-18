import asyncio
import logging
import os

import httpx
from sqlalchemy import select

from app.core import database as database_core
from app.core.config import ensure_postgresql_database_url, get_settings
from app.core.crypto import encrypt_secret
from app.core.migrations import run_migrations
from app.models.models import (
    AppAuthSettings,
    Endpoint,
    HeaderBlocklistRule,
    Profile,
    Provider,
    UserSetting,
)
from app.services.profile_invariants import ensure_profile_invariants

logger = logging.getLogger(__name__)

SKIP_STARTUP_SEQUENCE_ENV = "PRISM_SKIP_STARTUP_SEQUENCE"

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


async def seed_providers() -> None:
    async with database_core.AsyncSessionLocal() as session:
        result = await session.execute(select(Provider))
        existing = result.scalars().all()
        if not existing:
            for provider_data in DEFAULT_PROVIDERS:
                session.add(Provider(**provider_data))
            await session.commit()
            logger.info("Seeded default providers")


async def seed_profile_invariants() -> None:
    async with database_core.AsyncSessionLocal() as session:
        await ensure_profile_invariants(session)
        await session.commit()
        logger.info("Ensured default profile invariants")


async def seed_header_blocklist_rules() -> None:
    async with database_core.AsyncSessionLocal() as session:
        for default_rule in SYSTEM_BLOCKLIST_DEFAULTS:
            existing = (
                await session.execute(
                    select(HeaderBlocklistRule).where(
                        HeaderBlocklistRule.match_type == default_rule["match_type"],
                        HeaderBlocklistRule.pattern == default_rule["pattern"],
                        HeaderBlocklistRule.is_system == True,  # noqa: E712
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue
            session.add(
                HeaderBlocklistRule(
                    name=default_rule["name"],
                    match_type=default_rule["match_type"],
                    pattern=default_rule["pattern"],
                    enabled=True,
                    is_system=True,
                )
            )
        await session.commit()
        logger.info("Seeded system header blocklist rules")


async def seed_user_settings() -> None:
    async with database_core.AsyncSessionLocal() as session:
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
    async with database_core.AsyncSessionLocal() as session:
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
    async with database_core.AsyncSessionLocal() as session:
        endpoints = (
            (await session.execute(select(Endpoint).order_by(Endpoint.id.asc())))
            .scalars()
            .all()
        )
        updated_count = 0
        for endpoint in endpoints:
            encrypted = encrypt_secret(endpoint.api_key)
            if encrypted == endpoint.api_key:
                continue
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


async def run_startup_sequence() -> None:
    if os.getenv(SKIP_STARTUP_SEQUENCE_ENV) == "1":
        logger.info("Skipping startup bootstrap; launcher already applied it")
        return

    await run_startup_migrations()
    await seed_providers()
    await seed_profile_invariants()
    await seed_user_settings()
    await seed_app_auth_settings()
    await encrypt_endpoint_secrets()
    await seed_header_blocklist_rules()


def build_http_client() -> httpx.AsyncClient:
    settings = get_settings()
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=settings.connect_timeout,
            read=settings.read_timeout,
            write=settings.write_timeout,
            pool=5.0,
        ),
        limits=httpx.Limits(max_connections=20),
        follow_redirects=True,
    )


__all__ = [
    "DEFAULT_PROVIDERS",
    "SKIP_STARTUP_SEQUENCE_ENV",
    "SYSTEM_BLOCKLIST_DEFAULTS",
    "build_http_client",
    "encrypt_endpoint_secrets",
    "run_startup_migrations",
    "run_startup_sequence",
    "seed_app_auth_settings",
    "seed_header_blocklist_rules",
    "seed_profile_invariants",
    "seed_providers",
    "seed_user_settings",
]
