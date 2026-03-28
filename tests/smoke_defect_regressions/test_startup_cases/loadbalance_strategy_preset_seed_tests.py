from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.models import LoadbalanceStrategy, Profile


class TestDEF085_LoadbalanceStrategyPresetSeed:
    @pytest.mark.asyncio
    async def test_seed_loadbalance_strategy_preset_creates_failover_strategy_on_default_profile(
        self,
    ):
        from app.main import seed_profile_invariants
        from app.bootstrap import startup

        await seed_profile_invariants()
        await getattr(startup, "seed_loadbalance_strategy_preset")()
        await getattr(startup, "seed_loadbalance_strategy_preset")()

        async with AsyncSessionLocal() as session:
            default_profile = (
                await session.execute(
                    select(Profile)
                    .where(Profile.is_default.is_(True))
                    .order_by(Profile.id.asc())
                    .limit(1)
                )
            ).scalar_one()
            strategies = (
                (
                    await session.execute(
                        select(LoadbalanceStrategy)
                        .where(LoadbalanceStrategy.profile_id == default_profile.id)
                        .order_by(LoadbalanceStrategy.id.asc())
                    )
                )
                .scalars()
                .all()
            )

        preset_strategies = [
            strategy
            for strategy in strategies
            if strategy.name == startup.DEFAULT_LOADBALANCE_STRATEGY_PRESET_NAME
        ]

        assert default_profile.is_default is True
        assert len(preset_strategies) == 1
        assert preset_strategies[0].profile_id == default_profile.id
        assert preset_strategies[0].strategy_type == "failover"
        assert preset_strategies[0].failover_recovery_enabled is True
        assert preset_strategies[0].failover_status_codes == [
            403,
            422,
            429,
            500,
            502,
            503,
            504,
            529,
        ]

    @pytest.mark.asyncio
    async def test_run_startup_sequence_seeds_preset_after_profile_invariants(self):
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
                "app.bootstrap.startup.seed_loadbalance_strategy_preset",
                _record("strategy_preset"),
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
            "strategy_preset",
            "user_settings",
            "auth_settings",
            "encrypt_secrets",
            "header_blocklist",
        ]
