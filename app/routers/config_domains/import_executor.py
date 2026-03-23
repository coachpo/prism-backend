import json
from dataclasses import dataclass
from typing import Literal, cast

from fastapi import HTTPException
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.models.models import (
    Connection,
    Endpoint,
    EndpointFxRateSetting,
    HeaderBlocklistRule,
    ModelConfig,
    PricingTemplate,
    Provider,
    UserSetting,
)
from app.routers.shared import lock_profile_row
from app.schemas.schemas import ConfigImportRequest, ConfigImportResponse
from app.services.loadbalancer import clear_current_state_for_profile
from app.services.proxy_service import normalize_base_url


@dataclass
class _IdAllocator:
    next_id: int

    def take(self) -> int:
        allocated_id = self.next_id
        self.next_id += 1
        return allocated_id


async def _lock_import_target_tables(db: AsyncSession) -> None:
    await db.execute(
        text(
            "LOCK TABLE "
            "endpoint_fx_rate_settings, "
            "connections, "
            "endpoints, "
            "model_configs, "
            "pricing_templates, "
            "user_settings, "
            "header_blocklist_rules "
            "IN SHARE ROW EXCLUSIVE MODE"
        )
    )


async def _build_id_allocator(db: AsyncSession, model) -> _IdAllocator:
    max_id = await db.scalar(select(func.max(model.id)))
    return _IdAllocator(next_id=(max_id or 0) + 1)


async def _sync_id_sequence_if_present(db: AsyncSession, model) -> None:
    sequence_name = await db.scalar(
        select(func.pg_get_serial_sequence(model.__table__.fullname, "id"))
    )
    if sequence_name is None:
        return

    max_id = await db.scalar(select(func.max(model.id)))
    if max_id is None:
        return

    await db.execute(select(func.setval(sequence_name, max_id, True)))


def _sorted_import_connections(connections):
    return [
        connection
        for _, connection in sorted(
            enumerate(connections),
            key=lambda item: (
                item[1].priority,
                item[0],
            ),
        )
    ]


def _normalize_reference_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _resolve_endpoint_name(
    *,
    context: str,
    endpoint_id: int | None,
    endpoint_name: str | None,
    endpoint_name_to_id: dict[str, int],
    endpoint_id_to_name: dict[int, str],
) -> str:
    resolved_endpoint_name = _normalize_reference_name(endpoint_name)
    if resolved_endpoint_name is None and endpoint_id is None:
        raise HTTPException(
            status_code=400,
            detail=f"{context} must include endpoint_name or endpoint_id",
        )

    if endpoint_id is not None:
        mapped_endpoint_name = endpoint_id_to_name.get(endpoint_id)
        if mapped_endpoint_name is None:
            raise HTTPException(
                status_code=400,
                detail=f"{context} references unknown endpoint_id '{endpoint_id}'",
            )
        if resolved_endpoint_name is None:
            resolved_endpoint_name = mapped_endpoint_name
        elif resolved_endpoint_name != mapped_endpoint_name:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{context} endpoint_name '{resolved_endpoint_name}' does not match "
                    f"endpoint_id '{endpoint_id}'"
                ),
            )

    if resolved_endpoint_name is None:
        raise HTTPException(
            status_code=400,
            detail=f"{context} must include endpoint_name or endpoint_id",
        )

    if resolved_endpoint_name not in endpoint_name_to_id:
        raise HTTPException(
            status_code=400,
            detail=f"{context} references unknown endpoint_name '{resolved_endpoint_name}'",
        )

    return resolved_endpoint_name


def _resolve_pricing_template_id(
    *,
    context: str,
    pricing_template_id: int | None,
    pricing_template_name: str | None,
    pricing_template_name_to_id: dict[str, int],
    pricing_template_id_to_name: dict[int, str],
) -> int | None:
    resolved_pricing_template_name = _normalize_reference_name(pricing_template_name)
    if resolved_pricing_template_name is None and pricing_template_id is None:
        return None

    if pricing_template_id is not None:
        mapped_pricing_template_name = pricing_template_id_to_name.get(
            pricing_template_id
        )
        if mapped_pricing_template_name is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{context} references unknown pricing_template_id "
                    f"'{pricing_template_id}'"
                ),
            )
        if resolved_pricing_template_name is None:
            resolved_pricing_template_name = mapped_pricing_template_name
        elif resolved_pricing_template_name != mapped_pricing_template_name:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{context} pricing_template_name '{resolved_pricing_template_name}' "
                    f"does not match pricing_template_id '{pricing_template_id}'"
                ),
            )

    if resolved_pricing_template_name is None:
        return None

    mapped_pricing_template_id = pricing_template_name_to_id.get(
        resolved_pricing_template_name
    )
    if mapped_pricing_template_id is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{context} references unknown pricing_template_name "
                f"'{resolved_pricing_template_name}'"
            ),
        )

    return mapped_pricing_template_id


async def execute_import_payload(
    db: AsyncSession, *, profile_id: int, data: ConfigImportRequest
) -> ConfigImportResponse:
    await clear_current_state_for_profile(profile_id)
    await lock_profile_row(db, profile_id=profile_id)
    await _lock_import_target_tables(db)
    await db.execute(
        delete(EndpointFxRateSetting).where(
            EndpointFxRateSetting.profile_id == profile_id
        )
    )
    await db.execute(delete(Connection).where(Connection.profile_id == profile_id))
    await db.execute(delete(Endpoint).where(Endpoint.profile_id == profile_id))
    await db.execute(delete(ModelConfig).where(ModelConfig.profile_id == profile_id))
    await db.execute(
        delete(PricingTemplate).where(PricingTemplate.profile_id == profile_id)
    )
    await db.execute(
        delete(HeaderBlocklistRule).where(
            HeaderBlocklistRule.is_system == False,  # noqa: E712
            HeaderBlocklistRule.profile_id == profile_id,
        )
    )
    await db.flush()

    provider_types_needed = sorted({model.provider_type for model in data.models})
    provider_map: dict[str, int] = {}
    if provider_types_needed:
        providers = (
            (
                await db.execute(
                    select(Provider).where(
                        Provider.provider_type.in_(provider_types_needed)
                    )
                )
            )
            .scalars()
            .all()
        )
        provider_map = {provider.provider_type: provider.id for provider in providers}
        missing_provider_types = [
            provider_type
            for provider_type in provider_types_needed
            if provider_type not in provider_map
        ]
        if missing_provider_types:
            missing = ", ".join(sorted(missing_provider_types))
            raise HTTPException(
                status_code=400,
                detail=f"Missing provider types in system: {missing}",
            )

    endpoint_id_allocator = await _build_id_allocator(db, Endpoint)
    template_id_allocator = await _build_id_allocator(db, PricingTemplate)
    model_config_id_allocator = await _build_id_allocator(db, ModelConfig)
    connection_id_allocator = await _build_id_allocator(db, Connection)
    user_setting_id_allocator = await _build_id_allocator(db, UserSetting)
    fx_setting_id_allocator = await _build_id_allocator(db, EndpointFxRateSetting)
    header_rule_id_allocator = await _build_id_allocator(db, HeaderBlocklistRule)

    endpoint_name_to_id: dict[str, int] = {}
    endpoint_id_to_name: dict[int, str] = {}
    endpoints_count = 0
    sorted_endpoints = sorted(
        enumerate(data.endpoints),
        key=lambda item: (
            item[1].position if item[1].position is not None else item[0],
            item[0],
        ),
    )

    for normalized_position, (_, endpoint_data) in enumerate(sorted_endpoints):
        endpoint_name = endpoint_data.name.strip()
        endpoint = Endpoint(
            id=endpoint_id_allocator.take(),
            profile_id=profile_id,
            name=endpoint_name,
            base_url=normalize_base_url(endpoint_data.base_url),
            api_key=encrypt_secret(endpoint_data.api_key),
            position=normalized_position,
        )
        db.add(endpoint)
        await db.flush()

        endpoint_name_to_id[endpoint_name] = endpoint.id
        endpoint_import_id = endpoint_data.endpoint_id
        if endpoint_import_id is not None:
            endpoint_id_to_name[endpoint_import_id] = endpoint_name
        endpoints_count += 1

    pricing_template_name_to_id: dict[str, int] = {}
    pricing_template_id_to_name: dict[int, str] = {}
    templates_count = 0
    for template_data in data.pricing_templates:
        template_name = template_data.name.strip()
        template = PricingTemplate(
            id=template_id_allocator.take(),
            profile_id=profile_id,
            name=template_name,
            description=template_data.description,
            pricing_unit="PER_1M",
            pricing_currency_code=template_data.pricing_currency_code,
            input_price=template_data.input_price,
            output_price=template_data.output_price,
            cached_input_price=template_data.cached_input_price,
            cache_creation_price=template_data.cache_creation_price,
            reasoning_price=template_data.reasoning_price,
            missing_special_token_price_policy=cast(
                Literal["MAP_TO_OUTPUT", "ZERO_COST"],
                template_data.missing_special_token_price_policy,
            ),
            version=template_data.version,
        )
        db.add(template)
        await db.flush()

        pricing_template_name_to_id[template_name] = template.id
        template_import_id = template_data.pricing_template_id
        if template_import_id is not None:
            pricing_template_id_to_name[template_import_id] = template_name
        templates_count += 1

    connections_count = 0
    imported_connection_pairs: set[tuple[str, str]] = set()

    for model in data.models:
        is_proxy = model.model_type == "proxy"
        model_config = ModelConfig(
            id=model_config_id_allocator.take(),
            provider_id=provider_map[model.provider_type],
            profile_id=profile_id,
            model_id=model.model_id,
            display_name=model.display_name,
            model_type=cast(Literal["native", "proxy"], model.model_type),
            redirect_to=model.redirect_to if is_proxy else None,
            lb_strategy="single" if is_proxy else model.lb_strategy,
            failover_recovery_enabled=True
            if is_proxy
            else model.failover_recovery_enabled,
            failover_recovery_cooldown_seconds=60
            if is_proxy
            else model.failover_recovery_cooldown_seconds,
            is_enabled=model.is_enabled,
        )
        db.add(model_config)
        await db.flush()

        if is_proxy:
            continue

        for normalized_priority, connection_data in enumerate(
            _sorted_import_connections(model.connections)
        ):
            resolved_endpoint_name = _resolve_endpoint_name(
                context=f"Connection for model '{model.model_id}'",
                endpoint_id=connection_data.endpoint_id,
                endpoint_name=connection_data.endpoint_name,
                endpoint_name_to_id=endpoint_name_to_id,
                endpoint_id_to_name=endpoint_id_to_name,
            )
            mapped_endpoint_id = endpoint_name_to_id[resolved_endpoint_name]

            mapped_pricing_template_id = _resolve_pricing_template_id(
                context=f"Connection for model '{model.model_id}'",
                pricing_template_id=connection_data.pricing_template_id,
                pricing_template_name=connection_data.pricing_template_name,
                pricing_template_name_to_id=pricing_template_name_to_id,
                pricing_template_id_to_name=pricing_template_id_to_name,
            )

            connection = Connection(
                id=connection_id_allocator.take(),
                model_config_id=model_config.id,
                profile_id=profile_id,
                endpoint_id=mapped_endpoint_id,
                pricing_template_id=mapped_pricing_template_id,
                is_active=connection_data.is_active,
                priority=normalized_priority,
                name=connection_data.name,
                auth_type=cast(
                    Literal["openai", "anthropic", "gemini"] | None,
                    connection_data.auth_type,
                ),
                custom_headers=json.dumps(connection_data.custom_headers)
                if connection_data.custom_headers
                else None,
            )
            db.add(connection)
            connections_count += 1
            imported_connection_pairs.add((model.model_id, resolved_endpoint_name))
    await db.flush()

    user_settings = (
        await db.execute(
            select(UserSetting)
            .where(UserSetting.profile_id == profile_id)
            .order_by(UserSetting.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if user_settings is None:
        user_settings = UserSetting(
            id=user_setting_id_allocator.take(),
            profile_id=profile_id,
            report_currency_code="USD",
            report_currency_symbol="$",
        )
        db.add(user_settings)

    if data.user_settings is not None:
        user_settings.report_currency_code = data.user_settings.report_currency_code
        user_settings.report_currency_symbol = data.user_settings.report_currency_symbol
        for mapping in data.user_settings.endpoint_fx_mappings:
            resolved_endpoint_name = _resolve_endpoint_name(
                context=f"FX mapping for model '{mapping.model_id}'",
                endpoint_id=mapping.endpoint_id,
                endpoint_name=mapping.endpoint_name,
                endpoint_name_to_id=endpoint_name_to_id,
                endpoint_id_to_name=endpoint_id_to_name,
            )
            if (
                mapping.model_id,
                resolved_endpoint_name,
            ) not in imported_connection_pairs:
                continue
            mapped_endpoint_id = endpoint_name_to_id.get(resolved_endpoint_name)
            if mapped_endpoint_id is None:
                continue
            db.add(
                EndpointFxRateSetting(
                    id=fx_setting_id_allocator.take(),
                    model_id=mapping.model_id,
                    profile_id=profile_id,
                    endpoint_id=mapped_endpoint_id,
                    fx_rate=mapping.fx_rate,
                )
            )
    else:
        user_settings.report_currency_code = "USD"
        user_settings.report_currency_symbol = "$"

    for rule_data in sorted(
        data.header_blocklist_rules,
        key=lambda rule: (rule.match_type, rule.pattern, rule.name),
    ):
        db.add(
            HeaderBlocklistRule(
                id=header_rule_id_allocator.take(),
                name=rule_data.name,
                profile_id=profile_id,
                match_type=rule_data.match_type,
                pattern=rule_data.pattern,
                enabled=rule_data.enabled,
                is_system=False,
            )
        )
    await db.flush()
    for model in (
        Endpoint,
        PricingTemplate,
        ModelConfig,
        Connection,
        UserSetting,
        EndpointFxRateSetting,
        HeaderBlocklistRule,
    ):
        await _sync_id_sequence_if_present(db, model)

    return ConfigImportResponse(
        endpoints_imported=endpoints_count,
        models_imported=len(data.models),
        pricing_templates_imported=templates_count,
        connections_imported=connections_count,
    )


__all__ = ["execute_import_payload"]
