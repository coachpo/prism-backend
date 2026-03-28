from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.models.models import ModelConfig
from app.services.loadbalancer.state import clear_model_state
from app.schemas.schemas import (
    ModelConfigCreate,
    ModelConfigUpdate,
    ProxyTargetReference,
)

from .mutation_helpers import (
    apply_model_type_update_defaults,
    build_model_create_values,
    ensure_loadbalance_strategy_exists,
    ensure_vendor_exists,
    ensure_proxy_update_preconditions,
    ensure_valid_model_type,
    load_model_config_or_404,
    replace_proxy_targets,
    sync_renamed_model_references,
    validate_native_model_update,
)
from .query_helpers import (
    MODEL_CONFIG_DETAIL_OPTIONS,
    ensure_model_id_available,
    list_proxy_referrers,
    load_model_config_detail_or_404,
    validate_proxy_model,
)


async def create_model_config_record(
    db: AsyncSession,
    *,
    body: ModelConfigCreate,
    profile_id: int,
) -> ModelConfig:
    await ensure_vendor_exists(db, body.vendor_id)
    if body.model_type == "native" and body.loadbalance_strategy_id is not None:
        await ensure_loadbalance_strategy_exists(
            db,
            profile_id=profile_id,
            strategy_id=body.loadbalance_strategy_id,
        )
    await ensure_model_id_available(
        db,
        profile_id=profile_id,
        model_id=body.model_id,
    )

    model_type = ensure_valid_model_type(body.model_type or "native")
    target_models = await validate_proxy_model(
        db,
        profile_id=profile_id,
        model_type=model_type,
        proxy_targets=body.proxy_targets,
        api_family=body.api_family,
    )

    config = ModelConfig(
        **build_model_create_values(
            profile_id=profile_id,
            vendor_id=body.vendor_id,
            api_family=body.api_family,
            model_id=body.model_id,
            display_name=body.display_name,
            model_type=model_type,
            loadbalance_strategy_id=body.loadbalance_strategy_id,
            is_enabled=body.is_enabled,
        )
    )
    db.add(config)
    await db.flush()
    if model_type == "proxy":
        await replace_proxy_targets(
            db,
            config=config,
            target_models=target_models,
            proxy_targets=body.proxy_targets,
        )
        await db.flush()

    result = await db.execute(
        select(ModelConfig)
        .execution_options(populate_existing=True)
        .options(*MODEL_CONFIG_DETAIL_OPTIONS)
        .where(ModelConfig.id == config.id)
    )
    return result.scalar_one()


async def update_model_config_record(
    db: AsyncSession,
    *,
    model_config_id: int,
    body: ModelConfigUpdate,
    profile_id: int,
) -> ModelConfig:
    config = await load_model_config_detail_or_404(
        db,
        model_config_id=model_config_id,
        profile_id=profile_id,
    )
    original_model_id = config.model_id
    update_data = body.model_dump(exclude_unset=True)

    if "vendor_id" in update_data:
        await ensure_vendor_exists(db, update_data["vendor_id"])

    if "model_id" in update_data and update_data["model_id"] != config.model_id:
        await ensure_model_id_available(
            db,
            profile_id=profile_id,
            model_id=update_data["model_id"],
            exclude_id=config.id,
        )

    new_model_type = ensure_valid_model_type(
        update_data.get("model_type", config.model_type)
    )
    new_api_family = update_data.get("api_family", config.api_family)
    new_model_id = update_data.get("model_id", config.model_id)
    new_loadbalance_strategy_id = update_data.get(
        "loadbalance_strategy_id", config.loadbalance_strategy_id
    )
    new_proxy_targets: list[ProxyTargetReference] = (
        body.proxy_targets or []
        if "proxy_targets" in update_data
        else [
            ProxyTargetReference.model_validate(target)
            for target in config.proxy_targets
        ]
    )

    await validate_native_model_update(
        db,
        config=config,
        profile_id=profile_id,
        new_model_type=new_model_type,
        new_api_family=new_api_family,
    )
    ensure_proxy_update_preconditions(
        config=config,
        new_model_type=new_model_type,
        new_proxy_targets=new_proxy_targets,
        new_model_id=new_model_id,
    )

    target_models = await validate_proxy_model(
        db,
        profile_id=profile_id,
        model_type=new_model_type,
        proxy_targets=new_proxy_targets,
        api_family=new_api_family,
        exclude_model_id=config.model_id,
    )

    if new_model_type == "native":
        if new_loadbalance_strategy_id is None:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=400,
                detail="loadbalance_strategy_id is required for native models",
            )
        await ensure_loadbalance_strategy_exists(
            db,
            profile_id=profile_id,
            strategy_id=new_loadbalance_strategy_id,
        )
    elif update_data.get("loadbalance_strategy_id") is not None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=400,
            detail="loadbalance_strategy_id must be null for proxy models",
        )

    apply_model_type_update_defaults(update_data, model_type=new_model_type)
    update_data.pop("proxy_targets", None)

    clear_model_current_state = any(
        (
            "is_enabled" in update_data
            and update_data["is_enabled"] != config.is_enabled,
            "loadbalance_strategy_id" in update_data
            and update_data["loadbalance_strategy_id"]
            != config.loadbalance_strategy_id,
            "model_type" in update_data and new_model_type != config.model_type,
            "api_family" in update_data and new_api_family != config.api_family,
        )
    )

    for key, value in update_data.items():
        setattr(config, key, value)

    if new_model_type == "proxy":
        await replace_proxy_targets(
            db,
            config=config,
            target_models=target_models,
            proxy_targets=new_proxy_targets,
        )
    else:
        config.proxy_targets.clear()

    if "model_id" in update_data and update_data["model_id"] != original_model_id:
        await sync_renamed_model_references(
            db,
            profile_id=profile_id,
            config_id=config.id,
            original_model_id=original_model_id,
            new_model_id=update_data["model_id"],
            new_model_type=new_model_type,
        )

    if clear_model_current_state:
        await clear_model_state(profile_id, config.id)

    config_id = config.id
    config.updated_at = utc_now()
    await db.flush()
    db.expire(
        config,
        ["proxy_targets", "connections", "vendor", "loadbalance_strategy"],
    )

    result = await db.execute(
        select(ModelConfig)
        .execution_options(populate_existing=True)
        .options(*MODEL_CONFIG_DETAIL_OPTIONS)
        .where(ModelConfig.id == config_id)
    )
    return result.scalar_one()


async def delete_model_config_record(
    db: AsyncSession,
    *,
    model_config_id: int,
    profile_id: int,
) -> dict[str, bool]:
    config = await load_model_config_or_404(
        db,
        model_config_id=model_config_id,
        profile_id=profile_id,
    )

    if config.model_type == "native":
        referrer_list = await list_proxy_referrers(
            db,
            profile_id=profile_id,
            model_id=config.model_id,
        )
        if referrer_list:
            ids = ", ".join(referrer.model_id for referrer in referrer_list)
            from fastapi import HTTPException

            raise HTTPException(
                status_code=400,
                detail=f"Cannot delete: proxy models [{ids}] point to this model",
            )

    await clear_model_state(profile_id, config.id)
    await db.delete(config)
    await db.flush()
    return {"deleted": True}


__all__ = [
    "create_model_config_record",
    "delete_model_config_record",
    "update_model_config_record",
]
