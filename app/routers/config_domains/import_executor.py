import json
from dataclasses import dataclass
from typing import Any, Literal, cast

from fastapi import HTTPException
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import (
    decrypt_bundle_secret,
    encrypt_secret,
    get_bundle_secret_key_id,
)
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
from app.schemas.schemas import (
    ConfigImportPreviewResponse,
    ConfigImportRequest,
    ConfigImportResponse,
    ConfigImportVendorResolution,
)
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


def _get_vendor_hint_conflicting_fields(
    *, existing_vendor: Vendor, imported_vendor_ref
) -> list[str]:
    conflicting_fields: list[str] = []
    if (
        imported_vendor_ref.name_hint is not None
        and existing_vendor.name != imported_vendor_ref.name_hint
    ):
        conflicting_fields.append("name_hint")
    if (
        imported_vendor_ref.description_hint is not None
        and _normalize_optional_text(existing_vendor.description)
        != imported_vendor_ref.description_hint
    ):
        conflicting_fields.append("description_hint")
    if (
        imported_vendor_ref.icon_key_hint is not None
        and _normalize_icon_key(existing_vendor.icon_key)
        != imported_vendor_ref.icon_key_hint
    ):
        conflicting_fields.append("icon_key_hint")
    return conflicting_fields


def _resolve_new_vendor_name(imported_vendor_ref) -> str:
    return (imported_vendor_ref.name_hint or imported_vendor_ref.key).strip()


async def _preview_import_vendors(
    db: AsyncSession, *, vendor_payloads_by_key: dict[str, Any]
) -> tuple[dict[str, Vendor], list[ConfigImportVendorResolution], list[str]]:
    if not vendor_payloads_by_key:
        return {}, [], []

    imported_vendor_refs = list(vendor_payloads_by_key.values())

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
    proposed_new_vendor_names = [
        _resolve_new_vendor_name(vendor_ref)
        for vendor_ref in imported_vendor_refs
        if vendor_ref.key not in existing_vendors_by_key
    ]
    existing_name_vendors = []
    if proposed_new_vendor_names:
        existing_name_vendors = (
            (
                await db.execute(
                    select(Vendor).where(Vendor.name.in_(proposed_new_vendor_names))
                )
            )
            .scalars()
            .all()
        )
    existing_vendors_by_name = {vendor.name: vendor for vendor in existing_name_vendors}
    resolutions: list[ConfigImportVendorResolution] = []
    blocking_errors: list[str] = []
    proposed_name_to_key: dict[str, str] = {}

    for vendor_key, imported_vendor in vendor_payloads_by_key.items():
        existing_vendor = existing_vendors_by_key.get(vendor_key)
        if existing_vendor is None:
            proposed_name = _resolve_new_vendor_name(imported_vendor)
            duplicate_key = proposed_name_to_key.get(proposed_name)
            if duplicate_key is not None and duplicate_key != vendor_key:
                blocking_errors.append(
                    f"Config import would create duplicate global vendor name '{proposed_name}' for keys '{duplicate_key}' and '{vendor_key}'"
                )
            else:
                proposed_name_to_key[proposed_name] = vendor_key

            existing_name_vendor = existing_vendors_by_name.get(proposed_name)
            if (
                existing_name_vendor is not None
                and existing_name_vendor.key != vendor_key
            ):
                blocking_errors.append(
                    f"Config import vendor '{vendor_key}' would create global vendor name '{proposed_name}' that already exists on key '{existing_name_vendor.key}'"
                )

            resolutions.append(
                ConfigImportVendorResolution(
                    vendor_key=vendor_key,
                    resolution="create",
                )
            )
            continue

        conflicting_fields = _get_vendor_hint_conflicting_fields(
            existing_vendor=existing_vendor,
            imported_vendor_ref=imported_vendor,
        )
        resolutions.append(
            ConfigImportVendorResolution(
                vendor_key=vendor_key,
                resolution="reuse",
                warning=(
                    "Imported vendor hints differ from existing global vendor metadata "
                    f"for fields: {', '.join(conflicting_fields)}"
                    if conflicting_fields
                    else None
                ),
            )
        )

    return existing_vendors_by_key, resolutions, blocking_errors


def _decrypt_import_secret_payload(data: ConfigImportRequest) -> dict[str, str]:
    expected_key_id = get_bundle_secret_key_id()
    if data.secret_payload.key_id != expected_key_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Config import bundle key mismatch: "
                f"bundle key_id '{data.secret_payload.key_id}' does not match server key_id '{expected_key_id}'"
            ),
        )

    decrypted_by_ref: dict[str, str] = {}
    for entry in data.secret_payload.entries:
        try:
            if not entry.ciphertext.startswith("enc:"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Config import secret ref '{entry.ref}' must be encrypted",
                )
            decrypted_value = decrypt_bundle_secret(entry.ciphertext)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Config import could not decrypt secret ref '{entry.ref}'",
            ) from exc

        if not decrypted_value:
            raise HTTPException(
                status_code=400,
                detail=f"Config import secret ref '{entry.ref}' resolved to an empty value",
            )
        decrypted_by_ref[entry.ref] = decrypted_value
    return decrypted_by_ref


async def build_import_preview(
    db: AsyncSession, *, data: ConfigImportRequest
) -> ConfigImportPreviewResponse:
    vendor_payloads_by_key = {vendor.key: vendor for vendor in data.vendor_refs}
    _, vendor_resolutions, blocking_errors = await _preview_import_vendors(
        db, vendor_payloads_by_key=vendor_payloads_by_key
    )
    decrypted_by_ref = _decrypt_import_secret_payload(data)
    warnings = [
        resolution.warning
        for resolution in vendor_resolutions
        if resolution.warning is not None
    ]
    return ConfigImportPreviewResponse(
        ready=len(blocking_errors) == 0,
        version=2,
        bundle_kind="profile_config",
        endpoints_imported=len(data.endpoints),
        pricing_templates_imported=len(data.pricing_templates),
        strategies_imported=len(data.loadbalance_strategies),
        models_imported=len(data.models),
        connections_imported=sum(len(model.connections) for model in data.models),
        vendor_resolutions=vendor_resolutions,
        secret_key_id=data.secret_payload.key_id,
        decryptable_secret_refs=sorted(decrypted_by_ref.keys()),
        blocking_errors=blocking_errors,
        warnings=warnings,
    )


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
    vendor_payloads_by_key = {vendor.key: vendor for vendor in data.vendor_refs}
    existing_vendors_by_key, _, blocking_errors = await _preview_import_vendors(
        db, vendor_payloads_by_key=vendor_payloads_by_key
    )
    if blocking_errors:
        raise HTTPException(status_code=400, detail=blocking_errors[0])
    decrypted_secrets_by_ref = _decrypt_import_secret_payload(data)
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
                    name=vendor_data.name_hint or vendor_data.key,
                    description=vendor_data.description_hint,
                    icon_key=vendor_data.icon_key_hint,
                    audit_enabled=False,
                    audit_capture_bodies=True,
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
            api_key=(
                encrypt_secret(
                    decrypted_secrets_by_ref[endpoint_data.api_key_secret_ref]
                )
                if endpoint_data.api_key_secret_ref is not None
                else ""
            ),
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

    if data.profile_settings is not None:
        user_settings.report_currency_code = data.profile_settings.report_currency_code
        user_settings.report_currency_symbol = (
            data.profile_settings.report_currency_symbol
        )
        user_settings.timezone_preference = data.profile_settings.timezone_preference
        for mapping in data.profile_settings.endpoint_fx_mappings:
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
        user_settings.timezone_preference = None

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


__all__ = [
    "ConfigImportPreviewResponse",
    "ExecuteImportPayloadResult",
    "build_import_preview",
    "execute_import_payload",
]
