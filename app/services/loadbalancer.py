import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.models import ModelConfig, Endpoint

logger = logging.getLogger(__name__)

_rr_counters: dict[int, int] = {}


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

    if config.model_type == "redirect" and config.redirect_to:
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
                "Redirect target model_id=%r not found or disabled for redirect model_id=%r",
                config.redirect_to,
                model_id,
            )
            return None
        return target

    return config


def get_active_endpoints(model_config: ModelConfig) -> list[Endpoint]:
    return sorted(
        [ep for ep in model_config.endpoints if ep.is_active],
        key=lambda ep: ep.priority,
    )


def select_endpoint(model_config: ModelConfig) -> Endpoint | None:
    active = get_active_endpoints(model_config)
    if not active:
        return None

    strategy = model_config.lb_strategy

    if strategy == "single":
        return active[0]

    elif strategy == "round_robin":
        config_id = model_config.id
        if config_id not in _rr_counters:
            _rr_counters[config_id] = 0
        idx = _rr_counters[config_id] % len(active)
        _rr_counters[config_id] += 1
        return active[idx]

    elif strategy == "failover":
        return active[0]

    return active[0]


def get_failover_candidates(
    model_config: ModelConfig, failed_endpoint_id: int
) -> list[Endpoint]:
    active = get_active_endpoints(model_config)
    return [ep for ep in active if ep.id != failed_endpoint_id]
