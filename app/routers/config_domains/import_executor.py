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


async def execute_import_payload(
    db: AsyncSession, *, profile_id: int, data: ConfigImportRequest
) -> ConfigImportResponse:
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

    endpoint_id_map: dict[int, int] = {}
    endpoints_count = 0
    sorted_endpoints = sorted(
        enumerate(data.endpoints),
        key=lambda item: (
            item[1].position if item[1].position is not None else item[0],
            item[0],
        ),
    )

    for normalized_position, (_, endpoint_data) in enumerate(sorted_endpoints):
        endpoint = Endpoint(
            id=endpoint_id_allocator.take(),
            profile_id=profile_id,
            name=endpoint_data.name.strip(),
            base_url=normalize_base_url(endpoint_data.base_url),
            api_key=encrypt_secret(endpoint_data.api_key),
            position=normalized_position,
        )
        db.add(endpoint)
        await db.flush()

        endpoint_id_map[endpoint_data.endpoint_id] = endpoint.id
        endpoints_count += 1

    template_id_map: dict[int, int] = {}
    templates_count = 0
    for template_data in data.pricing_templates:
        template = PricingTemplate(
            id=template_id_allocator.take(),
            profile_id=profile_id,
            name=template_data.name.strip(),
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

        template_id_map[template_data.pricing_template_id] = template.id
        templates_count += 1

    connections_count = 0
    imported_connection_pairs: set[tuple[str, int]] = set()

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
            mapped_endpoint_id = endpoint_id_map.get(connection_data.endpoint_id)
            if mapped_endpoint_id is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Connection for model '{model.model_id}' references unknown "
                        f"endpoint_id '{connection_data.endpoint_id}'"
                    ),
                )

            connection = Connection(
                id=connection_id_allocator.take(),
                model_config_id=model_config.id,
                profile_id=profile_id,
                endpoint_id=mapped_endpoint_id,
                pricing_template_id=(
                    template_id_map[connection_data.pricing_template_id]
                    if connection_data.pricing_template_id is not None
                    else None
                ),
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
            imported_connection_pairs.add((model.model_id, connection_data.endpoint_id))
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
            if (mapping.model_id, mapping.endpoint_id) not in imported_connection_pairs:
                continue
            mapped_endpoint_id = endpoint_id_map.get(mapping.endpoint_id)
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
