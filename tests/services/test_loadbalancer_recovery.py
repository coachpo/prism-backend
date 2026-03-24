from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestLoadbalancerRecovery:
    def test_compute_base_cooldown_returns_auth_override_for_auth_like_failures(self):
        from app.services.loadbalancer.recovery import _compute_base_cooldown

        settings = SimpleNamespace(
            failover_auth_error_cooldown_seconds=900,
            failover_failure_threshold=3,
            failover_backoff_multiplier=2.0,
            failover_max_cooldown_seconds=3600,
        )

        with patch(
            "app.services.loadbalancer.recovery.get_loadbalancer_settings",
            return_value=settings,
        ):
            cooldown = _compute_base_cooldown(
                base_cooldown_seconds=30.0,
                consecutive_failures=1,
                failure_kind="auth_like",
            )

        assert cooldown == 900.0

    def test_compute_base_cooldown_returns_zero_before_threshold(self):
        from app.services.loadbalancer.recovery import _compute_base_cooldown

        settings = SimpleNamespace(
            failover_auth_error_cooldown_seconds=900,
            failover_failure_threshold=3,
            failover_backoff_multiplier=2.0,
            failover_max_cooldown_seconds=3600,
        )

        with patch(
            "app.services.loadbalancer.recovery.get_loadbalancer_settings",
            return_value=settings,
        ):
            cooldown = _compute_base_cooldown(
                base_cooldown_seconds=30.0,
                consecutive_failures=2,
                failure_kind="timeout",
            )

        assert cooldown == 0.0

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
            await record_connection_recovery(profile_id=3, connection_id=9)

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
                provider_id=1,
                now_at=None,
            )

        mark_probe_eligible_logged.assert_awaited_once_with(
            profile_id=3,
            connection_id=9,
            now_at=None,
        )
