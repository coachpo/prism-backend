from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.models import (
    Connection,
    Endpoint,
    LoadbalanceCurrentState,
    LoadbalanceRoundRobinState,
    LoadbalanceStrategy,
    ModelConfig,
    Profile,
    RoutingConnectionRuntimeState,
    Vendor,
)
from tests.loadbalance_strategy_helpers import (
    DEFAULT_FAILOVER_STATUS_CODES,
    make_auto_recovery_disabled,
    make_auto_recovery_enabled,
    make_routing_policy_adaptive,
)


def _policy(**overrides):
    from app.services.loadbalancer.policy import resolve_effective_loadbalance_policy

    routing_policy = make_routing_policy_adaptive(
        failure_status_codes=cast(
            list[int],
            overrides.get("failover_status_codes", DEFAULT_FAILOVER_STATUS_CODES),
        ),
        base_open_seconds=int(
            cast(float | int, overrides.get("failover_cooldown_seconds", 30.0))
        ),
        failure_threshold=cast(int, overrides.get("failover_failure_threshold", 3)),
        backoff_multiplier=float(
            cast(float | int, overrides.get("failover_backoff_multiplier", 2.0))
        ),
        max_open_seconds=cast(int, overrides.get("failover_max_cooldown_seconds", 900)),
        jitter_ratio=float(
            cast(float | int, overrides.get("failover_jitter_ratio", 0.2))
        ),
        ban_mode=cast(
            Literal["off", "manual", "temporary"],
            overrides.get("failover_ban_mode", "off"),
        ),
        max_open_strikes_before_ban=cast(
            int, overrides.get("failover_max_cooldown_strikes_before_ban", 0)
        ),
        ban_duration_seconds=cast(
            int, overrides.get("failover_ban_duration_seconds", 0)
        ),
    )
    return resolve_effective_loadbalance_policy(
        SimpleNamespace(routing_policy=routing_policy)
    )


def _legacy_policy(
    *,
    legacy_strategy_type: str = "single",
    auto_recovery: dict[str, object] | None = None,
):
    from app.services.loadbalancer.policy import resolve_effective_loadbalance_policy

    return resolve_effective_loadbalance_policy(
        SimpleNamespace(
            strategy_type="legacy",
            legacy_strategy_type=legacy_strategy_type,
            auto_recovery=auto_recovery or make_auto_recovery_enabled(),
        )
    )


def _vendor_key_for_api_family(api_family: str) -> str:
    return "google" if api_family == "gemini" else api_family


async def _get_or_create_vendor(db, *, api_family: str = "openai"):
    vendor_key = _vendor_key_for_api_family(api_family)
    vendor = (
        (
            await db.execute(
                select(Vendor)
                .where(Vendor.key == vendor_key)
                .order_by(Vendor.id.asc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if vendor is not None:
        return vendor

    vendor = Vendor(key=vendor_key, name="OpenAI recovery")
    db.add(vendor)
    await db.flush()
    return vendor


async def _create_connection_fixture(*, suffix: str) -> tuple[int, int]:
    async with AsyncSessionLocal() as session:
        vendor = await _get_or_create_vendor(session)
        profile = Profile(name=f"Recovery Profile {suffix}", is_active=False, version=0)
        strategy = LoadbalanceStrategy(
            profile=profile,
            name=f"recovery-strategy-{suffix}",
            routing_policy=make_routing_policy_adaptive(),
        )
        model = ModelConfig(
            profile=profile,
            vendor_id=vendor.id,
            api_family="openai",
            model_id=f"recovery-model-{suffix}",
            model_type="native",
            loadbalance_strategy=strategy,
            is_enabled=True,
        )
        endpoint = Endpoint(
            profile=profile,
            name=f"recovery-endpoint-{suffix}",
            base_url="https://recovery.example.com/v1",
            api_key="sk-recovery",
            position=0,
        )
        connection = Connection(
            profile=profile,
            model_config_rel=model,
            endpoint_rel=endpoint,
            is_active=True,
            priority=0,
            name=f"recovery-connection-{suffix}",
        )
        session.add_all([profile, strategy, model, endpoint, connection])
        await session.commit()
        await session.refresh(profile)
        await session.refresh(connection)
        return profile.id, connection.id


class TestLoadbalancerRecovery:
    def test_resolve_effective_loadbalance_policy_reads_adaptive_routing_policy_document(
        self,
    ):
        from app.core.config import get_settings
        from app.services.loadbalancer.policy import (
            resolve_effective_loadbalance_policy,
        )

        settings = get_settings()
        strategy = SimpleNamespace(
            routing_policy=make_routing_policy_adaptive(
                routing_objective="maximize_availability",
                failure_status_codes=[503, 429],
            ),
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
        assert policy.failover_status_codes == (429, 503)

    def test_resolve_effective_loadbalance_policy_defaults_adaptive_branch_values(
        self,
    ):
        from app.services.loadbalancer.policy import (
            resolve_effective_loadbalance_policy,
        )

        strategy = SimpleNamespace(routing_policy=make_routing_policy_adaptive())

        policy = resolve_effective_loadbalance_policy(strategy)

        assert policy.strategy_type == "adaptive"
        assert policy.failover_recovery_enabled is True
        assert policy.failover_ban_mode == "off"
        assert policy.failover_max_cooldown_strikes_before_ban == 0
        assert policy.failover_ban_duration_seconds == 0
        assert policy.monitoring_enabled is True

    @pytest.mark.asyncio
    async def test_record_loadbalance_event_persists_max_cooldown_strike_and_banned(
        self,
    ):
        from app.models.models import LoadbalanceEvent
        from app.services.audit_service import record_loadbalance_event

        suffix = uuid4().hex[:8]
        profile_id, connection_id = await _create_connection_fixture(suffix=suffix)

        await record_loadbalance_event(
            profile_id=profile_id,
            connection_id=connection_id,
            event_type="max_cooldown_strike",
            failure_kind="transient_http",
            consecutive_failures=3,
            cooldown_seconds=900.0,
            blocked_until_mono=1_741_234_567.0,
            model_id=f"recovery-model-{suffix}",
            endpoint_id=None,
            vendor_id=None,
            failure_threshold=2,
            backoff_multiplier=2.0,
            max_cooldown_seconds=900,
            max_cooldown_strikes=1,
            ban_mode="off",
            banned_until_at=None,
        )
        await record_loadbalance_event(
            profile_id=profile_id,
            connection_id=connection_id,
            event_type="banned",
            failure_kind="transient_http",
            consecutive_failures=3,
            cooldown_seconds=900.0,
            blocked_until_mono=1_741_234_568.0,
            model_id=f"recovery-model-{suffix}",
            endpoint_id=None,
            vendor_id=None,
            failure_threshold=2,
            backoff_multiplier=2.0,
            max_cooldown_seconds=900,
            max_cooldown_strikes=1,
            ban_mode="temporary",
            banned_until_at=None,
        )

        async with AsyncSessionLocal() as session:
            events = (
                (
                    await session.execute(
                        select(LoadbalanceEvent)
                        .where(LoadbalanceEvent.profile_id == profile_id)
                        .order_by(LoadbalanceEvent.id.asc())
                    )
                )
                .scalars()
                .all()
            )

        assert [event.event_type for event in events[-2:]] == [
            "max_cooldown_strike",
            "banned",
        ]

    def test_compute_base_cooldown_uses_effective_policy_values(self):
        from app.services.loadbalancer.recovery import _compute_base_cooldown

        policy = _policy(
            strategy_type="failover",
            failover_recovery_enabled=True,
            failover_cooldown_seconds=30.0,
            failover_failure_threshold=4,
            failover_backoff_multiplier=3.0,
            failover_max_cooldown_seconds=500,
            failover_jitter_ratio=0.2,
            failover_status_codes=[403, 429],
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

    def test_compute_base_cooldown_returns_zero_before_threshold(self):
        from app.services.loadbalancer.recovery import _compute_base_cooldown

        cooldown = _compute_base_cooldown(
            policy=_policy(
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
        from app.services.loadbalancer.recovery import _apply_jitter

        policy = _policy(
            strategy_type="failover",
            failover_recovery_enabled=True,
            failover_cooldown_seconds=30.0,
            failover_failure_threshold=2,
            failover_backoff_multiplier=2.0,
            failover_max_cooldown_seconds=900,
            failover_jitter_ratio=0.25,
            failover_status_codes=[429, 503],
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
                vendor_id=1,
                now_at=None,
            )

        mark_probe_eligible_logged.assert_awaited_once_with(
            profile_id=3,
            connection_id=9,
            now_at=None,
        )

    @pytest.mark.asyncio
    async def test_record_connection_failure_counts_max_cooldown_strike_once_per_capped_window(
        self,
    ):
        from datetime import datetime, timedelta, timezone

        from app.services.loadbalancer.recovery import record_connection_failure

        suffix = uuid4().hex[:8]
        profile_id, connection_id = await _create_connection_fixture(suffix=suffix)
        start = datetime(2026, 3, 26, 12, 0, tzinfo=timezone.utc)

        with patch(
            "app.services.loadbalancer.recovery.record_failed_transition"
        ) as record_failed_transition:
            await record_connection_failure(
                profile_id=profile_id,
                connection_id=connection_id,
                base_cooldown_seconds=30.0,
                failure_kind="transient_http",
                policy=_policy(
                    failover_failure_threshold=1,
                    failover_max_cooldown_seconds=30,
                    failover_jitter_ratio=0.0,
                    failover_ban_mode="temporary",
                    failover_max_cooldown_strikes_before_ban=3,
                    failover_ban_duration_seconds=300,
                ),
                now_at=start,
            )
            await record_connection_failure(
                profile_id=profile_id,
                connection_id=connection_id,
                base_cooldown_seconds=30.0,
                failure_kind="transient_http",
                policy=_policy(
                    failover_failure_threshold=1,
                    failover_max_cooldown_seconds=30,
                    failover_jitter_ratio=0.0,
                    failover_ban_mode="temporary",
                    failover_max_cooldown_strikes_before_ban=3,
                    failover_ban_duration_seconds=300,
                ),
                now_at=start + timedelta(seconds=1),
            )

        async with AsyncSessionLocal() as session:
            current_state = (
                await session.execute(
                    select(LoadbalanceCurrentState).where(
                        LoadbalanceCurrentState.profile_id == profile_id,
                        LoadbalanceCurrentState.connection_id == connection_id,
                    )
                )
            ).scalar_one()

        assert current_state.max_cooldown_strikes == 1
        assert current_state.ban_mode == "off"
        assert current_state.banned_until_at is None
        assert record_failed_transition.call_count == 2

    @pytest.mark.asyncio
    async def test_timeout_failure_does_not_increment_max_cooldown_strikes(self):
        from datetime import datetime, timezone

        from app.services.loadbalancer.recovery import record_connection_failure

        suffix = uuid4().hex[:8]
        profile_id, connection_id = await _create_connection_fixture(suffix=suffix)

        await record_connection_failure(
            profile_id=profile_id,
            connection_id=connection_id,
            base_cooldown_seconds=30.0,
            failure_kind="timeout",
            policy=_policy(
                failover_failure_threshold=1,
                failover_max_cooldown_seconds=30,
                failover_jitter_ratio=0.0,
                failover_ban_mode="temporary",
                failover_max_cooldown_strikes_before_ban=2,
                failover_ban_duration_seconds=300,
            ),
            now_at=datetime(2026, 3, 26, 13, 0, tzinfo=timezone.utc),
        )

        async with AsyncSessionLocal() as session:
            current_state = (
                await session.execute(
                    select(LoadbalanceCurrentState).where(
                        LoadbalanceCurrentState.profile_id == profile_id,
                        LoadbalanceCurrentState.connection_id == connection_id,
                    )
                )
            ).scalar_one()

        assert current_state.max_cooldown_strikes == 0
        assert current_state.ban_mode == "off"
        assert current_state.banned_until_at is None

    @pytest.mark.asyncio
    async def test_record_connection_failure_transitions_to_temporary_ban_at_strike_threshold(
        self,
    ):
        from datetime import datetime, timezone

        from app.services.loadbalancer.recovery import record_connection_failure

        suffix = uuid4().hex[:8]
        profile_id, connection_id = await _create_connection_fixture(suffix=suffix)
        now_at = datetime(2026, 3, 26, 14, 0, tzinfo=timezone.utc)

        await record_connection_failure(
            profile_id=profile_id,
            connection_id=connection_id,
            base_cooldown_seconds=30.0,
            failure_kind="transient_http",
            policy=_policy(
                failover_failure_threshold=1,
                failover_max_cooldown_seconds=30,
                failover_jitter_ratio=0.0,
                failover_ban_mode="temporary",
                failover_max_cooldown_strikes_before_ban=1,
                failover_ban_duration_seconds=300,
            ),
            now_at=now_at,
        )

        async with AsyncSessionLocal() as session:
            current_state = (
                await session.execute(
                    select(LoadbalanceCurrentState).where(
                        LoadbalanceCurrentState.profile_id == profile_id,
                        LoadbalanceCurrentState.connection_id == connection_id,
                    )
                )
            ).scalar_one()

        assert current_state.max_cooldown_strikes == 1
        assert current_state.ban_mode == "temporary"
        assert current_state.banned_until_at is not None

    @pytest.mark.asyncio
    async def test_record_connection_failure_skips_runtime_mutation_when_legacy_recovery_is_disabled(
        self,
    ):
        from datetime import datetime, timezone

        from app.services.loadbalancer.recovery import record_connection_failure

        suffix = uuid4().hex[:8]
        profile_id, connection_id = await _create_connection_fixture(suffix=suffix)

        with patch(
            "app.services.loadbalancer.recovery.record_failed_transition"
        ) as record_failed_transition:
            await record_connection_failure(
                profile_id=profile_id,
                connection_id=connection_id,
                base_cooldown_seconds=30.0,
                failure_kind="transient_http",
                policy=_legacy_policy(auto_recovery=make_auto_recovery_disabled()),
                now_at=datetime(2026, 3, 26, 14, 0, tzinfo=timezone.utc),
            )

        async with AsyncSessionLocal() as session:
            current_state = (
                await session.execute(
                    select(LoadbalanceCurrentState).where(
                        LoadbalanceCurrentState.profile_id == profile_id,
                        LoadbalanceCurrentState.connection_id == connection_id,
                    )
                )
            ).scalar_one_or_none()

        record_failed_transition.assert_not_called()
        assert current_state is None

    @pytest.mark.asyncio
    async def test_record_connection_recovery_preserves_monitoring_fields_when_runtime_row_is_not_empty(
        self,
    ):
        from datetime import datetime, timedelta, timezone

        from app.services.loadbalancer.recovery import record_connection_recovery

        suffix = uuid4().hex[:8]
        profile_id, connection_id = await _create_connection_fixture(suffix=suffix)
        now_at = datetime(2026, 3, 26, 14, 30, tzinfo=timezone.utc)

        async with AsyncSessionLocal() as session:
            session.add(
                RoutingConnectionRuntimeState(
                    profile_id=profile_id,
                    connection_id=connection_id,
                    consecutive_failures=3,
                    last_failure_kind="timeout",
                    last_cooldown_seconds=60.0,
                    blocked_until_at=now_at,
                    probe_eligible_logged=False,
                    circuit_state="open",
                    probe_available_at=now_at,
                    last_probe_status="healthy",
                    last_probe_at=now_at - timedelta(minutes=5),
                    endpoint_ping_ewma_ms=120.0,
                    conversation_delay_ewma_ms=240.0,
                )
            )
            await session.commit()

        await record_connection_recovery(
            profile_id=profile_id,
            connection_id=connection_id,
            policy=_policy(),
            model_id=f"recovery-model-{suffix}",
            endpoint_id=None,
            vendor_id=None,
        )

        async with AsyncSessionLocal() as session:
            runtime_state = (
                await session.execute(
                    select(RoutingConnectionRuntimeState).where(
                        RoutingConnectionRuntimeState.profile_id == profile_id,
                        RoutingConnectionRuntimeState.connection_id == connection_id,
                    )
                )
            ).scalar_one()

        assert runtime_state.consecutive_failures == 0
        assert runtime_state.last_failure_kind is None
        assert float(runtime_state.last_cooldown_seconds) == 0.0
        assert runtime_state.blocked_until_at is None
        assert runtime_state.ban_mode == "off"
        assert runtime_state.banned_until_at is None
        assert runtime_state.circuit_state == "closed"
        assert runtime_state.probe_available_at is None
        assert runtime_state.last_probe_status == "healthy"
        assert runtime_state.last_probe_at == now_at - timedelta(minutes=5)
        assert runtime_state.endpoint_ping_ewma_ms is not None
        assert runtime_state.conversation_delay_ewma_ms is not None
        assert float(runtime_state.endpoint_ping_ewma_ms) == pytest.approx(120.0)
        assert float(runtime_state.conversation_delay_ewma_ms) == pytest.approx(240.0)

    @pytest.mark.asyncio
    async def test_reset_connection_current_state_clears_ban_and_strike_state(self):
        from datetime import datetime, timezone

        from app.services.loadbalancer.admin import reset_connection_current_state

        suffix = uuid4().hex[:8]
        profile_id, connection_id = await _create_connection_fixture(suffix=suffix)

        async with AsyncSessionLocal() as session:
            session.add(
                LoadbalanceCurrentState(
                    profile_id=profile_id,
                    connection_id=connection_id,
                    consecutive_failures=5,
                    last_failure_kind="transient_http",
                    last_cooldown_seconds=60,
                    max_cooldown_strikes=2,
                    ban_mode="manual",
                    banned_until_at=None,
                    blocked_until_at=datetime(2026, 3, 26, 15, 0, tzinfo=timezone.utc),
                    probe_eligible_logged=False,
                )
            )
            model_config_id = (
                await session.execute(
                    select(Connection.model_config_id).where(
                        Connection.id == connection_id
                    )
                )
            ).scalar_one()
            session.add(
                LoadbalanceRoundRobinState(
                    profile_id=profile_id,
                    model_config_id=model_config_id,
                    next_cursor=2,
                )
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            response = await reset_connection_current_state(
                db=session,
                profile_id=profile_id,
                connection_id=connection_id,
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            current_state = (
                await session.execute(
                    select(LoadbalanceCurrentState).where(
                        LoadbalanceCurrentState.profile_id == profile_id,
                        LoadbalanceCurrentState.connection_id == connection_id,
                    )
                )
            ).scalar_one_or_none()
            round_robin_state = (
                await session.execute(
                    select(LoadbalanceRoundRobinState).where(
                        LoadbalanceRoundRobinState.profile_id == profile_id,
                        LoadbalanceRoundRobinState.model_config_id == model_config_id,
                    )
                )
            ).scalar_one_or_none()

        assert response.connection_id == connection_id
        assert response.cleared is True
        assert current_state is None
        assert round_robin_state is None
