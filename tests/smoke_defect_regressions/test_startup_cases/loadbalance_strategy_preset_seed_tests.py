from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.models import LoadbalanceStrategy, Profile
from app.services.loadbalancer.policy import (
    build_default_auto_recovery_document,
    build_default_routing_policy_document,
)


class TestDEF085_LoadbalanceStrategyPresetSeed:
    async def _get_default_profile(self) -> Profile:
        async with AsyncSessionLocal() as session:
            return (
                await session.execute(
                    select(Profile)
                    .where(Profile.is_default.is_(True))
                    .order_by(Profile.id.asc())
                    .limit(1)
                )
            ).scalar_one()

    async def _replace_default_profile_strategies(
        self, strategies: list[LoadbalanceStrategy]
    ) -> None:
        default_profile = await self._get_default_profile()
        async with AsyncSessionLocal() as session:
            existing = list(
                (
                    await session.execute(
                        select(LoadbalanceStrategy).where(
                            LoadbalanceStrategy.profile_id == default_profile.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            for strategy in existing:
                await session.delete(strategy)
            await session.flush()
            for strategy in strategies:
                strategy.profile_id = default_profile.id
                session.add(strategy)
            await session.commit()

    @pytest.mark.asyncio
    async def test_seed_loadbalance_strategy_presets_creates_both_defaults_idempotently(
        self,
    ):
        from app.main import seed_profile_invariants
        from app.bootstrap import startup

        await seed_profile_invariants()
        await self._replace_default_profile_strategies([])
        await getattr(startup, "seed_loadbalance_strategy_presets")()
        await getattr(startup, "seed_loadbalance_strategy_presets")()

        default_profile = await self._get_default_profile()
        async with AsyncSessionLocal() as session:
            strategies = list(
                (
                    await session.execute(
                        select(LoadbalanceStrategy)
                        .where(LoadbalanceStrategy.profile_id == default_profile.id)
                        .order_by(LoadbalanceStrategy.name.asc())
                    )
                )
                .scalars()
                .all()
            )

        assert default_profile.is_default is True
        assert {strategy.name for strategy in strategies} == {
            startup.DEFAULT_LEGACY_LOADBALANCE_STRATEGY_PRESET_NAME,
            startup.DEFAULT_ADAPTIVE_LOADBALANCE_STRATEGY_PRESET_NAME,
        }

        strategies_by_name = {strategy.name: strategy for strategy in strategies}
        legacy_strategy = strategies_by_name[
            startup.DEFAULT_LEGACY_LOADBALANCE_STRATEGY_PRESET_NAME
        ]
        adaptive_strategy = strategies_by_name[
            startup.DEFAULT_ADAPTIVE_LOADBALANCE_STRATEGY_PRESET_NAME
        ]
        assert legacy_strategy.strategy_type == "legacy"
        assert legacy_strategy.legacy_strategy_type == "round-robin"
        assert legacy_strategy.auto_recovery == build_default_auto_recovery_document()
        assert legacy_strategy.routing_policy is None

        assert adaptive_strategy.strategy_type == "adaptive"
        assert adaptive_strategy.legacy_strategy_type is None
        assert adaptive_strategy.auto_recovery is None
        assert (
            adaptive_strategy.routing_policy == build_default_routing_policy_document()
        )
        assert "monitoring" not in adaptive_strategy.routing_policy

    @pytest.mark.asyncio
    async def test_seed_loadbalance_strategy_presets_keeps_existing_adaptive_default_and_adds_missing_legacy(
        self,
    ):
        from app.main import seed_profile_invariants
        from app.bootstrap import startup

        await seed_profile_invariants()
        await self._replace_default_profile_strategies(
            [
                LoadbalanceStrategy(
                    name=startup.DEFAULT_ADAPTIVE_LOADBALANCE_STRATEGY_PRESET_NAME,
                    strategy_type="adaptive",
                    routing_policy=build_default_routing_policy_document(),
                )
            ]
        )

        await getattr(startup, "seed_loadbalance_strategy_presets")()

        default_profile = await self._get_default_profile()
        async with AsyncSessionLocal() as session:
            strategies = list(
                (
                    await session.execute(
                        select(LoadbalanceStrategy)
                        .where(LoadbalanceStrategy.profile_id == default_profile.id)
                        .order_by(LoadbalanceStrategy.name.asc())
                    )
                )
                .scalars()
                .all()
            )

        assert {strategy.name for strategy in strategies} == {
            startup.DEFAULT_ADAPTIVE_LOADBALANCE_STRATEGY_PRESET_NAME,
            startup.DEFAULT_LEGACY_LOADBALANCE_STRATEGY_PRESET_NAME,
        }

    @pytest.mark.asyncio
    async def test_seed_loadbalance_strategy_presets_replaces_old_default_failover_row_with_legacy_default(
        self,
    ):
        from app.main import seed_profile_invariants
        from app.bootstrap import startup

        await seed_profile_invariants()
        await self._replace_default_profile_strategies(
            [
                LoadbalanceStrategy(
                    name=startup.LEGACY_LOADBALANCE_STRATEGY_PRESET_NAME,
                    strategy_type="legacy",
                    legacy_strategy_type="fill-first",
                    auto_recovery=build_default_auto_recovery_document(),
                )
            ]
        )

        await getattr(startup, "seed_loadbalance_strategy_presets")()
        await getattr(startup, "seed_loadbalance_strategy_presets")()

        default_profile = await self._get_default_profile()
        async with AsyncSessionLocal() as session:
            strategies = list(
                (
                    await session.execute(
                        select(LoadbalanceStrategy)
                        .where(LoadbalanceStrategy.profile_id == default_profile.id)
                        .order_by(LoadbalanceStrategy.name.asc())
                    )
                )
                .scalars()
                .all()
            )

        assert {strategy.name for strategy in strategies} == {
            startup.DEFAULT_LEGACY_LOADBALANCE_STRATEGY_PRESET_NAME,
            startup.DEFAULT_ADAPTIVE_LOADBALANCE_STRATEGY_PRESET_NAME,
        }
        strategies_by_name = {strategy.name: strategy for strategy in strategies}
        legacy_strategy = strategies_by_name[
            startup.DEFAULT_LEGACY_LOADBALANCE_STRATEGY_PRESET_NAME
        ]
        assert legacy_strategy.strategy_type == "legacy"
        assert legacy_strategy.legacy_strategy_type == "round-robin"
        assert legacy_strategy.auto_recovery == build_default_auto_recovery_document()

    @pytest.mark.asyncio
    async def test_run_startup_sequence_seeds_presets_after_profile_invariants(self):
        from app.bootstrap.startup import run_startup_sequence

        calls: list[str] = []

        def _record(name: str) -> AsyncMock:
            return AsyncMock(side_effect=lambda: calls.append(name))

        with (
            patch(
                "app.bootstrap.startup.run_startup_migrations", _record("migrations")
            ),
            patch("app.bootstrap.startup.seed_vendors", _record("vendors")),
            patch(
                "app.bootstrap.startup.seed_profile_invariants",
                _record("profile_invariants"),
            ),
            patch(
                "app.bootstrap.startup.seed_loadbalance_strategy_presets",
                _record("strategy_presets"),
                create=True,
            ),
            patch("app.bootstrap.startup.seed_user_settings", _record("user_settings")),
            patch(
                "app.bootstrap.startup.seed_app_auth_settings",
                _record("auth_settings"),
            ),
            patch(
                "app.bootstrap.startup.encrypt_endpoint_secrets",
                _record("encrypt_secrets"),
            ),
            patch(
                "app.bootstrap.startup.seed_header_blocklist_rules",
                _record("header_blocklist"),
            ),
        ):
            await run_startup_sequence()

        assert calls == [
            "migrations",
            "vendors",
            "profile_invariants",
            "strategy_presets",
            "user_settings",
            "auth_settings",
            "encrypt_secrets",
            "header_blocklist",
        ]
