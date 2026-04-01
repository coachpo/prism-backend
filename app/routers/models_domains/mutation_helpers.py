from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    EndpointFxRateSetting,
    LoadbalanceStrategy,
    ModelConfig,
    ModelProxyTarget,
    Vendor,
)
from app.schemas.schemas import ProxyTargetReference

from .query_helpers import list_proxy_referrers

VALID_MODEL_TYPES = ("native", "proxy")


async def ensure_loadbalance_strategy_exists(
    db: AsyncSession,
    *,
    profile_id: int,
    strategy_id: int,
) -> LoadbalanceStrategy:
    result = await db.execute(
        select(LoadbalanceStrategy).where(
            LoadbalanceStrategy.profile_id == profile_id,
            LoadbalanceStrategy.id == strategy_id,
        )
    )
    strategy = result.scalar_one_or_none()
    if strategy is None:
        raise HTTPException(status_code=400, detail="Loadbalance strategy not found")
    return strategy


async def ensure_vendor_exists(db: AsyncSession, vendor_id: int) -> None:
    vendor = await db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=400, detail="Vendor not found")


def ensure_valid_model_type(model_type: str) -> str:
    if model_type not in VALID_MODEL_TYPES:
        raise HTTPException(
            status_code=400,
            detail="model_type must be 'native' or 'proxy'",
        )
    return model_type


async def validate_native_model_update(
    db: AsyncSession,
    *,
    config: ModelConfig,
    profile_id: int,
    new_model_type: str,
    new_api_family: str,
) -> None:
    if config.model_type != "native":
        return

    referrer_list = await list_proxy_referrers(
        db,
        profile_id=profile_id,
        model_id=config.model_id,
        exclude_id=config.id,
    )
    if not referrer_list:
        return

    ids = ", ".join(referrer.model_id for referrer in referrer_list)
    if new_model_type != "native":
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot convert native model to proxy while proxy models "
                f"[{ids}] point to it"
            ),
        )
    if new_api_family != config.api_family:
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot change api_family for native model while proxy models "
                f"[{ids}] point to it"
            ),
        )


def ensure_proxy_update_preconditions(
    *,
    config: ModelConfig,
    new_model_type: str,
    new_proxy_targets: list[ProxyTargetReference],
    new_model_id: str,
) -> None:
    if (
        new_model_type == "proxy"
        and config.model_type != "proxy"
        and len(config.connections) > 0
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot convert native model with connections to proxy. "
                "Delete connections first."
            ),
        )
    if any(
        proxy_target.target_model_id == new_model_id
        for proxy_target in new_proxy_targets
    ):
        raise HTTPException(
            status_code=400,
            detail="Proxy model cannot target itself",
        )


def build_model_create_values(
    *,
    profile_id: int,
    vendor_id: int,
    api_family: str,
    model_id: str,
    display_name: str | None,
    model_type: str,
    loadbalance_strategy_id: int | None,
    is_enabled: bool,
) -> dict[str, object]:
    return {
        "profile_id": profile_id,
        "vendor_id": vendor_id,
        "api_family": api_family,
        "model_id": model_id,
        "display_name": resolve_persisted_display_name(
            model_id=model_id,
            display_name=display_name,
        ),
        "model_type": model_type,
        "loadbalance_strategy_id": (
            None if model_type == "proxy" else loadbalance_strategy_id
        ),
        "is_enabled": is_enabled,
    }


def resolve_persisted_display_name(*, model_id: str, display_name: str | None) -> str:
    if display_name is None or not display_name.strip():
        return model_id
    return display_name


def apply_model_type_update_defaults(
    update_data: dict[str, object],
    *,
    model_type: str,
) -> None:
    if model_type == "proxy":
        update_data["loadbalance_strategy_id"] = None


async def sync_renamed_model_references(
    db: AsyncSession,
    *,
    profile_id: int,
    config_id: int,
    original_model_id: str,
    new_model_id: str,
    new_model_type: str,
) -> None:
    await db.execute(
        update(EndpointFxRateSetting)
        .where(
            EndpointFxRateSetting.profile_id == profile_id,
            EndpointFxRateSetting.model_id == original_model_id,
        )
        .values(model_id=new_model_id)
    )


async def replace_proxy_targets(
    db: AsyncSession,
    *,
    config: ModelConfig,
    target_models: list[ModelConfig],
    proxy_targets: list[ProxyTargetReference],
) -> None:
    await db.execute(
        delete(ModelProxyTarget).where(
            ModelProxyTarget.source_model_config_id == config.id
        )
    )
    for proxy_target, target in zip(proxy_targets, target_models, strict=True):
        db.add(
            ModelProxyTarget(
                source_model_config_id=config.id,
                target_model_config_id=target.id,
                position=proxy_target.position,
            )
        )


async def load_model_config_or_404(
    db: AsyncSession,
    *,
    model_config_id: int,
    profile_id: int,
) -> ModelConfig:
    result = await db.execute(
        select(ModelConfig).where(
            ModelConfig.id == model_config_id,
            ModelConfig.profile_id == profile_id,
        )
    )
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail="Model configuration not found")
    return config


__all__ = [
    "apply_model_type_update_defaults",
    "build_model_create_values",
    "ensure_loadbalance_strategy_exists",
    "ensure_vendor_exists",
    "ensure_proxy_update_preconditions",
    "ensure_valid_model_type",
    "load_model_config_or_404",
    "replace_proxy_targets",
    "resolve_persisted_display_name",
    "sync_renamed_model_references",
    "validate_native_model_update",
]
