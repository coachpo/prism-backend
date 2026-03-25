from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _policy(**overrides):
    from app.services.loadbalancer.policy import EffectiveLoadbalancePolicy

    return EffectiveLoadbalancePolicy(
        strategy_type=cast(
            Literal["single", "failover"],
            overrides.get("strategy_type", "failover"),
        ),
        failover_recovery_enabled=cast(
            bool, overrides.get("failover_recovery_enabled", True)
        ),
        failover_cooldown_seconds=float(
            cast(float | int, overrides.get("failover_cooldown_seconds", 30.0))
        ),
        failover_failure_threshold=cast(
            int, overrides.get("failover_failure_threshold", 3)
        ),
        failover_backoff_multiplier=float(
            cast(float | int, overrides.get("failover_backoff_multiplier", 2.0))
        ),
        failover_max_cooldown_seconds=cast(
            int, overrides.get("failover_max_cooldown_seconds", 900)
        ),
        failover_jitter_ratio=float(
            cast(float | int, overrides.get("failover_jitter_ratio", 0.2))
        ),
        failover_auth_error_cooldown_seconds=cast(
            int, overrides.get("failover_auth_error_cooldown_seconds", 1800)
        ),
    )


class TestLoadbalancerRecovery:
    def test_resolve_effective_loadbalance_policy_fills_legacy_strategy_defaults(self):
        from app.core.config import get_settings
        from app.services.loadbalancer.policy import (
            resolve_effective_loadbalance_policy,
        )

        settings = get_settings()
        strategy = SimpleNamespace(
            strategy_type="failover",
            failover_recovery_enabled=True,
            failover_cooldown_seconds=None,
            failover_failure_threshold=None,
            failover_backoff_multiplier=None,
            failover_max_cooldown_seconds=None,
            failover_jitter_ratio=None,
            failover_auth_error_cooldown_seconds=None,
        )

        policy = resolve_effective_loadbalance_policy(strategy)

        assert policy.failover_cooldown_seconds == float(
            settings.failover_cooldown_seconds
        )
        assert policy.failover_failure_threshold == settings.failover_failure_threshold
        assert policy.failover_backoff_multiplier == pytest.approx(
            settings.failover_backoff_multiplier
        )
        assert policy.failover_max_cooldown_seconds == (
            settings.failover_max_cooldown_seconds
        )
        assert policy.failover_jitter_ratio == pytest.approx(
            settings.failover_jitter_ratio
        )
        assert policy.failover_auth_error_cooldown_seconds == (
            settings.failover_auth_error_cooldown_seconds
        )

    def test_compute_base_cooldown_uses_effective_policy_values(self):
        from app.services.loadbalancer.policy import EffectiveLoadbalancePolicy
        from app.services.loadbalancer.recovery import _compute_base_cooldown

        policy = EffectiveLoadbalancePolicy(
            strategy_type="failover",
            failover_recovery_enabled=True,
            failover_cooldown_seconds=30.0,
            failover_failure_threshold=4,
            failover_backoff_multiplier=3.0,
            failover_max_cooldown_seconds=500,
            failover_jitter_ratio=0.2,
            failover_auth_error_cooldown_seconds=777,
        )

        assert (
            _compute_base_cooldown(
                policy=policy,
                base_cooldown_seconds=policy.failover_cooldown_seconds,
                consecutive_failures=3,
                failure_kind="timeout",
            )
            == 0.0
        )
        assert (
            _compute_base_cooldown(
                policy=policy,
                base_cooldown_seconds=policy.failover_cooldown_seconds,
                consecutive_failures=4,
                failure_kind="timeout",
            )
            == 30.0
        )
        assert (
            _compute_base_cooldown(
                policy=policy,
                base_cooldown_seconds=policy.failover_cooldown_seconds,
                consecutive_failures=1,
                failure_kind="auth_like",
            )
            == 777.0
        )

    def test_compute_base_cooldown_returns_auth_override_for_auth_like_failures(self):
        from app.services.loadbalancer.recovery import _compute_base_cooldown

        cooldown = _compute_base_cooldown(
            policy=_policy(
                failover_auth_error_cooldown_seconds=900,
                failover_failure_threshold=3,
                failover_backoff_multiplier=2.0,
                failover_max_cooldown_seconds=3600,
            ),
            base_cooldown_seconds=30.0,
            consecutive_failures=1,
            failure_kind="auth_like",
        )

        assert cooldown == 900.0

    def test_compute_base_cooldown_returns_zero_before_threshold(self):
        from app.services.loadbalancer.recovery import _compute_base_cooldown

        cooldown = _compute_base_cooldown(
            policy=_policy(
                failover_auth_error_cooldown_seconds=900,
                failover_failure_threshold=3,
                failover_backoff_multiplier=2.0,
                failover_max_cooldown_seconds=3600,
            ),
            base_cooldown_seconds=30.0,
            consecutive_failures=2,
            failure_kind="timeout",
        )

        assert cooldown == 0.0

    def test_apply_jitter_uses_effective_policy_ratio(self):
        from app.services.loadbalancer.policy import EffectiveLoadbalancePolicy
        from app.services.loadbalancer.recovery import _apply_jitter

        policy = EffectiveLoadbalancePolicy(
            strategy_type="failover",
            failover_recovery_enabled=True,
            failover_cooldown_seconds=30.0,
            failover_failure_threshold=2,
            failover_backoff_multiplier=2.0,
            failover_max_cooldown_seconds=900,
            failover_jitter_ratio=0.25,
            failover_auth_error_cooldown_seconds=1800,
        )

        with patch(
            "app.services.loadbalancer.recovery.random.uniform",
            return_value=1.25,
        ) as random_uniform:
            cooldown = _apply_jitter(40.0, policy=policy)

        random_uniform.assert_called_once_with(0.75, 1.25)
        assert cooldown == 50.0

    @pytest.mark.asyncio
    async def test_record_connection_recovery_returns_without_state_row(self):
        from app.services.loadbalancer.recovery import record_connection_recovery

        session = AsyncMock()
        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=execute_result)
        session.rollback = AsyncMock()
        session.delete = AsyncMock()
        session.commit = AsyncMock()

        session_context = AsyncMock()
        session_context.__aenter__ = AsyncMock(return_value=session)
        session_context.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "app.services.loadbalancer.recovery.AsyncSessionLocal",
            return_value=session_context,
        ):
            await record_connection_recovery(
                profile_id=3,
                connection_id=9,
                policy=_policy(),
            )

        session.rollback.assert_awaited_once()
        session.delete.assert_not_awaited()
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_claim_probe_eligible_returns_without_transition_when_state_not_claimed(
        self,
    ):
        from app.services.loadbalancer.recovery import claim_probe_eligible

        with patch(
            "app.services.loadbalancer.recovery.mark_probe_eligible_logged",
            AsyncMock(return_value=None),
        ) as mark_probe_eligible_logged:
            await claim_probe_eligible(
                profile_id=3,
                connection_id=9,
                model_id="gpt-test",
                endpoint_id=12,
                policy=_policy(),
                provider_id=1,
                now_at=None,
            )

        mark_probe_eligible_logged.assert_awaited_once_with(
            profile_id=3,
            connection_id=9,
            now_at=None,
        )
