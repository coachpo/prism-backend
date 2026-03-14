from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import EndpointFxRateSetting, ModelConfig, Provider

from .query_helpers import list_proxy_referrers

VALID_MODEL_TYPES = ("native", "proxy")
PROXY_LB_STRATEGY = "single"
PROXY_RECOVERY_ENABLED = True
PROXY_RECOVERY_COOLDOWN_SECONDS = 60


async def ensure_provider_exists(db: AsyncSession, provider_id: int) -> None:
    provider = await db.get(Provider, provider_id)
    if provider is None:
        raise HTTPException(status_code=400, detail="Provider not found")


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
    new_provider_id: int,
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
    if new_provider_id != config.provider_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot change provider for native model while proxy models "
                f"[{ids}] point to it"
            ),
        )


def ensure_proxy_update_preconditions(
    *,
    config: ModelConfig,
    new_model_type: str,
    new_redirect_to: str | None,
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
    if new_model_type == "proxy" and new_redirect_to == new_model_id:
        raise HTTPException(
            status_code=400,
            detail="Proxy model cannot redirect to itself",
        )


def build_model_create_values(
    *,
    profile_id: int,
    provider_id: int,
    model_id: str,
    display_name: str | None,
    model_type: str,
    redirect_to: str | None,
    lb_strategy: str,
    failover_recovery_enabled: bool,
    failover_recovery_cooldown_seconds: int,
    is_enabled: bool,
) -> dict[str, object]:
    return {
        "profile_id": profile_id,
        "provider_id": provider_id,
        "model_id": model_id,
        "display_name": display_name,
        "model_type": model_type,
        "redirect_to": redirect_to if model_type == "proxy" else None,
        "lb_strategy": PROXY_LB_STRATEGY if model_type == "proxy" else lb_strategy,
        "failover_recovery_enabled": (
            PROXY_RECOVERY_ENABLED
            if model_type == "proxy"
            else failover_recovery_enabled
        ),
        "failover_recovery_cooldown_seconds": (
            PROXY_RECOVERY_COOLDOWN_SECONDS
            if model_type == "proxy"
            else failover_recovery_cooldown_seconds
        ),
        "is_enabled": is_enabled,
    }


def apply_model_type_update_defaults(
    update_data: dict[str, object],
    *,
    model_type: str,
) -> None:
    if model_type == "native":
        update_data["redirect_to"] = None
        return

    update_data["lb_strategy"] = PROXY_LB_STRATEGY
    update_data["failover_recovery_enabled"] = PROXY_RECOVERY_ENABLED
    update_data["failover_recovery_cooldown_seconds"] = PROXY_RECOVERY_COOLDOWN_SECONDS


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
    if new_model_type != "native":
        return

    await db.execute(
        update(ModelConfig)
        .where(
            ModelConfig.profile_id == profile_id,
            ModelConfig.redirect_to == original_model_id,
            ModelConfig.id != config_id,
        )
        .values(redirect_to=new_model_id)
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
    "PROXY_LB_STRATEGY",
    "PROXY_RECOVERY_COOLDOWN_SECONDS",
    "PROXY_RECOVERY_ENABLED",
    "apply_model_type_update_defaults",
    "build_model_create_values",
    "ensure_provider_exists",
    "ensure_proxy_update_preconditions",
    "ensure_valid_model_type",
    "load_model_config_or_404",
    "sync_renamed_model_references",
    "validate_native_model_update",
]
