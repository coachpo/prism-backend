from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.models import Connection, ModelConfig
from app.services.loadbalancer_support.state import logger

MODEL_CONFIG_WITH_CONNECTION_OPTIONS = (
    selectinload(ModelConfig.connections).selectinload(Connection.endpoint_rel),
    selectinload(ModelConfig.connections).selectinload(Connection.pricing_template_rel),
    selectinload(ModelConfig.provider),
)


def _build_model_config_query(profile_id: int, model_id: str):
    return (
        select(ModelConfig)
        .options(*MODEL_CONFIG_WITH_CONNECTION_OPTIONS)
        .where(
            ModelConfig.profile_id == profile_id,
            ModelConfig.model_id == model_id,
            ModelConfig.is_enabled.is_(True),
        )
    )


async def _load_enabled_model_config(
    db: AsyncSession,
    *,
    profile_id: int,
    model_id: str,
) -> ModelConfig | None:
    result = await db.execute(_build_model_config_query(profile_id, model_id))
    return result.scalar_one_or_none()


async def get_model_config_with_connections(
    db: AsyncSession,
    profile_id: int,
    model_id: str,
) -> ModelConfig | None:
    config = await _load_enabled_model_config(
        db,
        profile_id=profile_id,
        model_id=model_id,
    )
    if config is None:
        return None

    if config.model_type != "proxy" or not config.redirect_to:
        return config

    target = await _load_enabled_model_config(
        db,
        profile_id=profile_id,
        model_id=config.redirect_to,
    )
    if target is None:
        logger.warning(
            "Proxy target model_id=%r not found or disabled for profile_id=%d proxy model_id=%r",
            config.redirect_to,
            profile_id,
            model_id,
        )
        return None
    return target


__all__ = ["get_model_config_with_connections"]
