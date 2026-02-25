import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.models import ModelConfig, Endpoint

logger = logging.getLogger(__name__)

# Recovery state: endpoint_id -> (blocked_until_mono, cooldown_seconds)
_recovery_state: dict[int, tuple[float, float]] = {}


async def get_model_config_with_endpoints(
    db: AsyncSession, model_id: str
) -> ModelConfig | None:
    result = await db.execute(
        select(ModelConfig)
        .options(
            selectinload(ModelConfig.endpoints), selectinload(ModelConfig.provider)
        )
        .where(ModelConfig.model_id == model_id, ModelConfig.is_enabled == True)
    )
    config = result.scalar_one_or_none()
    if not config:
        return None

    if config.model_type == "proxy" and config.redirect_to:
        target_result = await db.execute(
            select(ModelConfig)
            .options(
                selectinload(ModelConfig.endpoints),
                selectinload(ModelConfig.provider),
            )
            .where(
                ModelConfig.model_id == config.redirect_to,
                ModelConfig.is_enabled == True,
            )
        )
        target = target_result.scalar_one_or_none()
        if not target:
            logger.warning(
                "Proxy target model_id=%r not found or disabled for proxy model_id=%r",
                config.redirect_to,
                model_id,
            )
            return None
        return target

    return config


def get_active_endpoints(model_config: ModelConfig) -> list[Endpoint]:
    active_eps = [ep for ep in model_config.endpoints if ep.is_active]
    logger.debug(
        f"get_active_endpoints for model {model_config.model_id}: "
        f"{len(active_eps)}/{len(model_config.endpoints)} active "
        f"(filtered out: {[ep.id for ep in model_config.endpoints if not ep.is_active]})"
    )
    return sorted(active_eps, key=lambda ep: ep.priority)


def build_attempt_plan(model_config: ModelConfig, now_mono: float) -> list[Endpoint]:
    """Build ordered list of endpoints to try.

    For 'single' strategy: returns only the highest-priority active endpoint.
    For 'failover' strategy: returns healthy endpoints first (priority order),
    then probe-eligible endpoints (cooldown expired) at the end.
    """
    active = get_active_endpoints(model_config)
    if not active:
        logger.warning(
            f"build_attempt_plan: No active endpoints for model {model_config.model_id}"
        )
        return []

    if model_config.lb_strategy == "single":
        logger.debug(
            f"build_attempt_plan: single strategy, using endpoint {active[0].id}"
        )
        return [active[0]]

    # failover without recovery should always try all active endpoints.
    if not model_config.failover_recovery_enabled:
        logger.debug(
            f"build_attempt_plan: failover without recovery, trying {len(active)} endpoints"
        )
        return active

    # failover strategy with recovery enabled
    healthy: list[Endpoint] = []
    probe_eligible: list[Endpoint] = []

    for ep in active:
        state = _recovery_state.get(ep.id)
        if state is None:
            # Not in recovery — healthy
            healthy.append(ep)
        else:
            blocked_until, _ = state
            if now_mono >= blocked_until:
                # Cooldown expired — eligible for half-open probe
                probe_eligible.append(ep)
            # else: still cooling down — skip entirely

    logger.debug(
        f"build_attempt_plan: failover with recovery, "
        f"healthy={[ep.id for ep in healthy]}, "
        f"probe_eligible={[ep.id for ep in probe_eligible]}"
    )
    return healthy + probe_eligible


def mark_endpoint_failed(
    endpoint_id: int, cooldown_seconds: float, now_mono: float
) -> None:
    """Record an endpoint failure with cooldown period."""
    blocked_until = now_mono + cooldown_seconds
    _recovery_state[endpoint_id] = (blocked_until, cooldown_seconds)
    logger.info(
        "Endpoint %d marked failed, cooldown %.0fs, blocked until mono=%.1f",
        endpoint_id,
        cooldown_seconds,
        blocked_until,
    )


def mark_endpoint_recovered(endpoint_id: int) -> None:
    """Clear failure state for an endpoint after a successful probe."""
    if endpoint_id in _recovery_state:
        del _recovery_state[endpoint_id]
        logger.info("Endpoint %d recovered, removed from recovery state", endpoint_id)
