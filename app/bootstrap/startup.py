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
    LoadbalanceStrategy,
    Profile,
    UserSetting,
    Vendor,
)
from app.services.loadbalancer.policy import (
    build_default_auto_recovery_document,
    build_default_routing_policy_document,
)
from app.services.monitoring.scheduler import (
    DEFAULT_MONITORING_PROBE_INTERVAL_SECONDS,
)
from app.services.profile_invariants import ensure_profile_invariants

logger = logging.getLogger(__name__)

SKIP_STARTUP_SEQUENCE_ENV = "PRISM_SKIP_STARTUP_SEQUENCE"

DEFAULT_VENDORS = [
    {
        "key": "openai",
        "name": "OpenAI",
        "description": "OpenAI API (GPT models)",
        "icon_key": "openai",
    },
    {
        "key": "anthropic",
        "name": "Anthropic",
        "description": "Anthropic API (Claude models)",
        "icon_key": "anthropic",
    },
    {
        "key": "google",
        "name": "Google",
        "description": "Google Gemini API",
        "icon_key": "google",
    },
]

SYSTEM_BLOCKLIST_DEFAULTS: list[dict[str, str]] = [
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

DEFAULT_ADAPTIVE_LOADBALANCE_STRATEGY_PRESET_NAME = "Default adaptive routing"
DEFAULT_LEGACY_LOADBALANCE_STRATEGY_PRESET_NAME = "Default legacy routing"
DEFAULT_LOADBALANCE_STRATEGY_PRESET_NAME = (
    DEFAULT_ADAPTIVE_LOADBALANCE_STRATEGY_PRESET_NAME
)
LEGACY_LOADBALANCE_STRATEGY_PRESET_NAME = "Default failover"


async def seed_vendors() -> None:
    async with database_core.AsyncSessionLocal() as session:
        existing_vendors = (
            (await session.execute(select(Vendor).order_by(Vendor.id.asc())))
            .scalars()
            .all()
        )
        existing_keys = {vendor.key for vendor in existing_vendors}

        created_count = 0
        for vendor_data in DEFAULT_VENDORS:
            if vendor_data["key"] in existing_keys:
                continue
            session.add(Vendor(**vendor_data))
            created_count += 1

        if created_count > 0:
            await session.commit()
            logger.info("Seeded %d default vendors", created_count)


async def seed_profile_invariants() -> None:
    async with database_core.AsyncSessionLocal() as session:
        _ = await ensure_profile_invariants(session)
        await session.commit()
        logger.info("Ensured default profile invariants")


def _canonicalize_seeded_legacy_strategy(strategy: LoadbalanceStrategy) -> None:
    strategy.name = DEFAULT_LEGACY_LOADBALANCE_STRATEGY_PRESET_NAME
    strategy.strategy_type = "legacy"
    strategy.legacy_strategy_type = "round-robin"
    strategy.auto_recovery = build_default_auto_recovery_document()
    strategy.routing_policy = None


def _canonicalize_seeded_adaptive_strategy(strategy: LoadbalanceStrategy) -> None:
    strategy.name = DEFAULT_ADAPTIVE_LOADBALANCE_STRATEGY_PRESET_NAME
    strategy.strategy_type = "adaptive"
    strategy.legacy_strategy_type = None
    strategy.auto_recovery = None
    strategy.routing_policy = build_default_routing_policy_document()


async def seed_loadbalance_strategy_presets() -> None:
    async with database_core.AsyncSessionLocal() as session:
        default_profile = (
            await session.execute(
                select(Profile)
                .where(Profile.is_default.is_(True), Profile.deleted_at.is_(None))
                .order_by(Profile.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if default_profile is None:
            return

        existing_strategies = list(
            (
                await session.execute(
                    select(LoadbalanceStrategy)
                    .where(
                        LoadbalanceStrategy.profile_id == default_profile.id,
                        LoadbalanceStrategy.name.in_(
                            [
                                DEFAULT_ADAPTIVE_LOADBALANCE_STRATEGY_PRESET_NAME,
                                DEFAULT_LEGACY_LOADBALANCE_STRATEGY_PRESET_NAME,
                                LEGACY_LOADBALANCE_STRATEGY_PRESET_NAME,
                            ]
                        ),
                    )
                    .order_by(LoadbalanceStrategy.id.asc())
                )
            )
            .scalars()
            .all()
        )
        adaptive_strategy = next(
            (
                strategy
                for strategy in existing_strategies
                if strategy.name == DEFAULT_ADAPTIVE_LOADBALANCE_STRATEGY_PRESET_NAME
            ),
            None,
        )
        legacy_strategy = next(
            (
                strategy
                for strategy in existing_strategies
                if strategy.name == DEFAULT_LEGACY_LOADBALANCE_STRATEGY_PRESET_NAME
            ),
            None,
        )
        old_failover_strategy = next(
            (
                strategy
                for strategy in existing_strategies
                if strategy.name == LEGACY_LOADBALANCE_STRATEGY_PRESET_NAME
            ),
            None,
        )

        changed = False

        if adaptive_strategy is None:
            adaptive_strategy = LoadbalanceStrategy(profile_id=default_profile.id)
            session.add(adaptive_strategy)
            changed = True
        _canonicalize_seeded_adaptive_strategy(adaptive_strategy)

        if legacy_strategy is None and old_failover_strategy is not None:
            legacy_strategy = old_failover_strategy
            changed = True

        if legacy_strategy is None:
            legacy_strategy = LoadbalanceStrategy(profile_id=default_profile.id)
            session.add(legacy_strategy)
            changed = True
        _canonicalize_seeded_legacy_strategy(legacy_strategy)

        if (
            old_failover_strategy is not None
            and old_failover_strategy is not legacy_strategy
        ):
            await session.delete(old_failover_strategy)
            changed = True

        await session.flush()
        if changed:
            await session.commit()
            logger.info(
                "Seeded default loadbalance strategy presets for profile %d",
                default_profile.id,
            )


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

        existing_settings = list(
            (
                await session.execute(
                    select(UserSetting).where(UserSetting.profile_id.in_(profile_ids))
                )
            )
            .scalars()
            .all()
        )
        existing_profile_ids: set[int] = set()
        for settings in existing_settings:
            if hasattr(settings, "profile_id"):
                existing_profile_ids.add(settings.profile_id)
            elif isinstance(settings, int) and not isinstance(settings, bool):
                existing_profile_ids.add(settings)
        missing_profile_ids = [
            profile_id
            for profile_id in profile_ids
            if profile_id not in existing_profile_ids
        ]
        repaired_monitoring_defaults = 0

        for settings_row in existing_settings:
            if not hasattr(settings_row, "monitoring_probe_interval_seconds"):
                continue
            interval_seconds = getattr(
                settings_row, "monitoring_probe_interval_seconds", None
            )
            if interval_seconds is not None:
                continue
            settings_row.monitoring_probe_interval_seconds = (
                DEFAULT_MONITORING_PROBE_INTERVAL_SECONDS
            )
            repaired_monitoring_defaults += 1

        for profile_id in missing_profile_ids:
            session.add(
                UserSetting(
                    profile_id=profile_id,
                    report_currency_code="USD",
                    report_currency_symbol="$",
                    timezone_preference=None,
                    monitoring_probe_interval_seconds=(
                        DEFAULT_MONITORING_PROBE_INTERVAL_SECONDS
                    ),
                )
            )

        if missing_profile_ids or repaired_monitoring_defaults:
            await session.commit()
            logger.info(
                "Seeded default user settings for %d profile(s) and repaired monitoring cadence for %d profile(s)",
                len(missing_profile_ids),
                repaired_monitoring_defaults,
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
    await seed_vendors()
    await seed_profile_invariants()
    await seed_loadbalance_strategy_presets()
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
    "DEFAULT_VENDORS",
    "DEFAULT_LOADBALANCE_STRATEGY_PRESET_NAME",
    "SKIP_STARTUP_SEQUENCE_ENV",
    "SYSTEM_BLOCKLIST_DEFAULTS",
    "build_http_client",
    "encrypt_endpoint_secrets",
    "run_startup_migrations",
    "run_startup_sequence",
    "seed_app_auth_settings",
    "seed_header_blocklist_rules",
    "seed_loadbalance_strategy_presets",
    "seed_profile_invariants",
    "seed_vendors",
    "seed_user_settings",
]
