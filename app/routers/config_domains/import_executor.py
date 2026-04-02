import json
from dataclasses import dataclass
from typing import Any, Literal, cast

from fastapi import HTTPException
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.models.models import (
    Connection,
    Endpoint,
    EndpointFxRateSetting,
    HeaderBlocklistRule,
    LoadbalanceStrategy,
    ModelConfig,
    ModelProxyTarget,
    PricingTemplate,
    UserSetting,
    Vendor,
)
from app.routers.shared import lock_profile_row
from app.schemas.schemas import ConfigImportRequest, ConfigImportResponse
from app.services.loadbalancer.policy import (
    canonicalize_auto_recovery_document,
    canonicalize_routing_policy_document,
)
from app.services.loadbalancer.runtime_store import clear_profile_runtime_state
from app.services.proxy_service import normalize_base_url


@dataclass
class _IdAllocator:
    next_id: int

    def take(self) -> int:
        allocated_id = self.next_id
        self.next_id += 1
        return allocated_id


@dataclass(frozen=True)
class ExecuteImportPayloadResult:
    response: ConfigImportResponse
    imported_connection_ids: tuple[int, ...]


async def _lock_import_target_tables(db: AsyncSession) -> None:
    await db.execute(
        text(
            "LOCK TABLE "
            "endpoint_fx_rate_settings, "
            "connections, "
            "endpoints, "
            "loadbalance_strategies, "
            "model_configs, "
            "pricing_templates, "
            "vendors, "
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


def _normalize_optional_text(value: str | None) -> str | None:
    return _normalize_reference_name(value)


def _normalize_icon_key(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def _get_vendor_conflicting_fields(
    *, existing_vendor: Vendor, imported_vendor
) -> list[str]:
    conflicting_fields: list[str] = []
    if existing_vendor.name != imported_vendor.name:
        conflicting_fields.append("name")
    if (
        _normalize_optional_text(existing_vendor.description)
        != imported_vendor.description
    ):
        conflicting_fields.append("description")
    if existing_vendor.audit_enabled != imported_vendor.audit_enabled:
        conflicting_fields.append("audit_enabled")
    if existing_vendor.audit_capture_bodies != imported_vendor.audit_capture_bodies:
        conflicting_fields.append("audit_capture_bodies")
    if _normalize_icon_key(existing_vendor.icon_key) != imported_vendor.icon_key:
        conflicting_fields.append("icon_key")
    return conflicting_fields


async def _preflight_import_vendors(
    db: AsyncSession, *, vendor_payloads_by_key: dict[str, Any]
) -> dict[str, Vendor]:
    if not vendor_payloads_by_key:
        return {}

    existing_vendors = (
        (
            await db.execute(
                select(Vendor).where(Vendor.key.in_(vendor_payloads_by_key.keys()))
            )
        )
        .scalars()
        .all()
    )
    existing_vendors_by_key = {vendor.key: vendor for vendor in existing_vendors}

    for vendor_key, imported_vendor in vendor_payloads_by_key.items():
        existing_vendor = existing_vendors_by_key.get(vendor_key)
        if existing_vendor is None:
            continue

        conflicting_fields = _get_vendor_conflicting_fields(
            existing_vendor=existing_vendor,
            imported_vendor=imported_vendor,
        )
        if conflicting_fields:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Config import vendor '{vendor_key}' conflicts with existing global vendor metadata "
                    f"for fields: {', '.join(conflicting_fields)}"
                ),
            )

    return existing_vendors_by_key


def _resolve_endpoint_name(
    *,
    context: str,
    endpoint_name: str | None,
    endpoint_name_to_id: dict[str, int],
) -> str:
    resolved_endpoint_name = _normalize_reference_name(endpoint_name)
    if resolved_endpoint_name is None:
        raise HTTPException(
            status_code=400,
            detail=f"{context} must include endpoint_name",
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
    pricing_template_name: str | None,
    pricing_template_name_to_id: dict[str, int],
) -> int | None:
    resolved_pricing_template_name = _normalize_reference_name(pricing_template_name)
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
) -> ExecuteImportPayloadResult:
    await lock_profile_row(db, profile_id=profile_id)
    await _lock_import_target_tables(db)
    vendor_payloads_by_key = {vendor.key: vendor for vendor in data.vendors}
    existing_vendors_by_key = await _preflight_import_vendors(
        db, vendor_payloads_by_key=vendor_payloads_by_key
    )
    await clear_profile_runtime_state(session=db, profile_id=profile_id)
    profile_model_ids = select(ModelConfig.id).where(
        ModelConfig.profile_id == profile_id
    )
    await db.execute(
        delete(ModelProxyTarget).where(
            ModelProxyTarget.source_model_config_id.in_(profile_model_ids)
            | ModelProxyTarget.target_model_config_id.in_(profile_model_ids)
        )
    )
    await db.execute(
        delete(EndpointFxRateSetting).where(
            EndpointFxRateSetting.profile_id == profile_id
        )
    )
    await db.execute(delete(Connection).where(Connection.profile_id == profile_id))
    await db.execute(delete(Endpoint).where(Endpoint.profile_id == profile_id))
    await db.execute(delete(ModelConfig).where(ModelConfig.profile_id == profile_id))
    await db.execute(
        delete(LoadbalanceStrategy).where(LoadbalanceStrategy.profile_id == profile_id)
    )
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

    vendor_map: dict[str, int] = {}
    if vendor_payloads_by_key:
        for vendor_key, vendor_data in vendor_payloads_by_key.items():
            existing_vendor = existing_vendors_by_key.get(vendor_key)
            if existing_vendor is None:
                existing_vendor = Vendor(
                    key=vendor_data.key,
                    name=vendor_data.name,
                    description=vendor_data.description,
                    icon_key=vendor_data.icon_key,
                    audit_enabled=vendor_data.audit_enabled,
                    audit_capture_bodies=vendor_data.audit_capture_bodies,
                )
                db.add(existing_vendor)
                await db.flush()
                existing_vendors_by_key[vendor_key] = existing_vendor
            vendor_map[vendor_key] = existing_vendor.id

    endpoint_id_allocator = await _build_id_allocator(db, Endpoint)
    template_id_allocator = await _build_id_allocator(db, PricingTemplate)
    strategy_id_allocator = await _build_id_allocator(db, LoadbalanceStrategy)
    model_config_id_allocator = await _build_id_allocator(db, ModelConfig)
    connection_id_allocator = await _build_id_allocator(db, Connection)
    user_setting_id_allocator = await _build_id_allocator(db, UserSetting)
    fx_setting_id_allocator = await _build_id_allocator(db, EndpointFxRateSetting)
    header_rule_id_allocator = await _build_id_allocator(db, HeaderBlocklistRule)

    endpoint_name_to_id: dict[str, int] = {}
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
        endpoints_count += 1

    pricing_template_name_to_id: dict[str, int] = {}
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
        templates_count += 1

    strategy_name_to_id: dict[str, int] = {}
    strategies_count = 0
    for strategy_data in data.loadbalance_strategies:
        strategy_name = strategy_data.name.strip()
        strategy = LoadbalanceStrategy(
            id=strategy_id_allocator.take(),
            profile_id=profile_id,
            name=strategy_name,
            strategy_type=strategy_data.strategy_type,
            legacy_strategy_type=(
                strategy_data.legacy_strategy_type
                if strategy_data.strategy_type == "legacy"
                else None
            ),
            auto_recovery=(
                canonicalize_auto_recovery_document(strategy_data.auto_recovery)
                if strategy_data.strategy_type == "legacy"
                else None
            ),
            routing_policy=(
                canonicalize_routing_policy_document(strategy_data.routing_policy)
                if strategy_data.strategy_type == "adaptive"
                else None
            ),
        )
        db.add(strategy)
        await db.flush()

        strategy_name_to_id[strategy_name] = strategy.id
        strategies_count += 1

    connections_count = 0
    imported_connection_ids: list[int] = []
    imported_connection_pairs: set[tuple[str, str]] = set()
    proxy_target_specs: list[tuple[int, list[Any]]] = []
    model_id_to_config_id: dict[str, int] = {}

    for model in data.models:
        is_proxy = model.model_type == "proxy"
        model_config = ModelConfig(
            id=model_config_id_allocator.take(),
            vendor_id=vendor_map[model.vendor_key],
            profile_id=profile_id,
            api_family=model.api_family,
            model_id=model.model_id,
            display_name=model.display_name,
            model_type=cast(Literal["native", "proxy"], model.model_type),
            loadbalance_strategy_id=(
                None
                if is_proxy
                else strategy_name_to_id[cast(str, model.loadbalance_strategy_name)]
            ),
            is_enabled=model.is_enabled,
        )
        db.add(model_config)
        await db.flush()
        model_id_to_config_id[model.model_id] = model_config.id

        if is_proxy:
            proxy_target_specs.append((model_config.id, model.proxy_targets))
            continue

        for normalized_priority, connection_data in enumerate(
            _sorted_import_connections(model.connections)
        ):
            resolved_endpoint_name = _resolve_endpoint_name(
                context=f"Connection for model '{model.model_id}'",
                endpoint_name=connection_data.endpoint_name,
                endpoint_name_to_id=endpoint_name_to_id,
            )
            mapped_endpoint_id = endpoint_name_to_id[resolved_endpoint_name]

            mapped_pricing_template_id = _resolve_pricing_template_id(
                context=f"Connection for model '{model.model_id}'",
                pricing_template_name=connection_data.pricing_template_name,
                pricing_template_name_to_id=pricing_template_name_to_id,
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
                qps_limit=connection_data.qps_limit,
                max_in_flight_non_stream=connection_data.max_in_flight_non_stream,
                max_in_flight_stream=connection_data.max_in_flight_stream,
            )
            db.add(connection)
            connections_count += 1
            imported_connection_ids.append(connection.id)
            imported_connection_pairs.add((model.model_id, resolved_endpoint_name))

    for source_model_config_id, proxy_targets in proxy_target_specs:
        for proxy_target in proxy_targets:
            db.add(
                ModelProxyTarget(
                    source_model_config_id=source_model_config_id,
                    target_model_config_id=model_id_to_config_id[
                        proxy_target.target_model_id
                    ],
                    position=proxy_target.position,
                )
            )
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
                endpoint_name=mapping.endpoint_name,
                endpoint_name_to_id=endpoint_name_to_id,
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
        LoadbalanceStrategy,
        ModelConfig,
        Connection,
        UserSetting,
        EndpointFxRateSetting,
        HeaderBlocklistRule,
    ):
        await _sync_id_sequence_if_present(db, model)

    return ExecuteImportPayloadResult(
        response=ConfigImportResponse(
            endpoints_imported=endpoints_count,
            models_imported=len(data.models),
            pricing_templates_imported=templates_count,
            strategies_imported=strategies_count,
            connections_imported=connections_count,
        ),
        imported_connection_ids=tuple(imported_connection_ids),
    )


__all__ = ["ExecuteImportPayloadResult", "execute_import_payload"]
