from typing import Annotated
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.time import utc_now
from app.dependencies import get_db, get_effective_profile_id
from app.models.models import (
    Connection,
    Provider,
    ModelConfig,
    Endpoint,
    HeaderBlocklistRule,
    UserSetting,
    EndpointFxRateSetting,
    PricingTemplate,
)
from app.schemas.schemas import (
    ConfigExportResponse,
    ConfigModelExport,
    ConfigConnectionExport,
    ConfigEndpointExport,
    ConfigPricingTemplateExport,
    ConfigUserSettingsExport,
    ConfigEndpointFxRateExport,
    ConfigImportRequest,
    ConfigImportResponse,
    HeaderBlocklistRuleCreate,
    HeaderBlocklistRuleUpdate,
    HeaderBlocklistRuleResponse,
    HeaderBlocklistRuleExport,
)
from app.services.proxy_service import normalize_base_url, validate_base_url

logger = logging.getLogger(__name__)

router = APIRouter()

VALID_PROVIDER_TYPES = {"openai", "anthropic", "gemini"}


def _normalize_custom_headers_for_export(
    custom_headers: str | None,
) -> dict[str, str] | None:
    if custom_headers is None:
        return None
    try:
        decoded = json.loads(custom_headers)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(decoded, dict) and len(decoded) > 0:
        return decoded
    return None


@router.get("/export")
async def export_config(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    endpoints = (
        (
            await db.execute(
                select(Endpoint)
                .where(Endpoint.profile_id == profile_id)
                .order_by(Endpoint.position.asc(), Endpoint.id.asc())
            )
        )
        .scalars()
        .all()
    )
    model_configs = (
        (
            await db.execute(
                select(ModelConfig)
                .options(
                    selectinload(ModelConfig.connections).selectinload(
                        Connection.endpoint_rel
                    ),
                    selectinload(ModelConfig.connections).selectinload(
                        Connection.pricing_template_rel
                    ),
                )
                .where(ModelConfig.profile_id == profile_id)
                .order_by(ModelConfig.id.asc())
            )
        )
        .scalars()
        .all()
    )
    pricing_templates = (
        (
            await db.execute(
                select(PricingTemplate)
                .where(PricingTemplate.profile_id == profile_id)
                .order_by(PricingTemplate.id.asc())
            )
        )
        .scalars()
        .all()
    )

    provider_ids = {model.provider_id for model in model_configs}
    provider_type_map: dict[int, str] = {}
    if provider_ids:
        providers = (
            (await db.execute(select(Provider).where(Provider.id.in_(provider_ids))))
            .scalars()
            .all()
        )
        provider_type_map = {
            provider.id: provider.provider_type for provider in providers
        }

    exported_endpoints: list[ConfigEndpointExport] = []
    for endpoint in endpoints:
        exported_endpoints.append(
            ConfigEndpointExport(
                endpoint_id=endpoint.id,
                name=endpoint.name,
                base_url=endpoint.base_url,
                api_key=endpoint.api_key,
                position=endpoint.position,
            )
        )

    exported_pricing_templates: list[ConfigPricingTemplateExport] = []
    for template in pricing_templates:
        exported_pricing_templates.append(
            ConfigPricingTemplateExport(
                pricing_template_id=template.id,
                name=template.name,
                description=template.description,
                pricing_unit="PER_1M",
                pricing_currency_code=template.pricing_currency_code,
                input_price=template.input_price,
                output_price=template.output_price,
                cached_input_price=template.cached_input_price,
                cache_creation_price=template.cache_creation_price,
                reasoning_price=template.reasoning_price,
                missing_special_token_price_policy=template.missing_special_token_price_policy,
                version=template.version,
            )
        )
    exported_models: list[ConfigModelExport] = []
    for model in model_configs:
        sorted_connections = sorted(
            model.connections,
            key=lambda c: (c.priority, c.id),
        )
        exported_connections: list[ConfigConnectionExport] = []
        for connection in sorted_connections:
            exported_connections.append(
                ConfigConnectionExport(
                    connection_id=connection.id,
                    endpoint_id=connection.endpoint_id,
                    pricing_template_id=connection.pricing_template_id,
                    is_active=connection.is_active,
                    priority=connection.priority,
                    name=connection.name,
                    auth_type=connection.auth_type,
                    custom_headers=_normalize_custom_headers_for_export(
                        connection.custom_headers
                    ),
                )
            )
        exported_models.append(
            ConfigModelExport(
                provider_type=provider_type_map.get(model.provider_id, ""),
                model_id=model.model_id,
                display_name=model.display_name,
                model_type=model.model_type,
                redirect_to=model.redirect_to,
                lb_strategy="failover" if model.lb_strategy == "failover" else "single",
                failover_recovery_enabled=model.failover_recovery_enabled,
                failover_recovery_cooldown_seconds=model.failover_recovery_cooldown_seconds,
                is_enabled=model.is_enabled,
                connections=exported_connections,
            )
        )

    user_settings = (
        await db.execute(
            select(UserSetting)
            .where(UserSetting.profile_id == profile_id)
            .order_by(UserSetting.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    fx_mappings = (
        (
            await db.execute(
                select(EndpointFxRateSetting)
                .where(EndpointFxRateSetting.profile_id == profile_id)
                .order_by(
                    EndpointFxRateSetting.model_id.asc(),
                    EndpointFxRateSetting.endpoint_id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    header_blocklist_rules = (
        (
            await db.execute(
                select(HeaderBlocklistRule)
                .where(
                    HeaderBlocklistRule.is_system == False,  # noqa: E712
                    HeaderBlocklistRule.profile_id == profile_id,
                )
                .order_by(
                    HeaderBlocklistRule.match_type.asc(),
                    HeaderBlocklistRule.pattern.asc(),
                    HeaderBlocklistRule.name.asc(),
                    HeaderBlocklistRule.id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )

    data = ConfigExportResponse(
        version=2,
        exported_at=utc_now(),
        endpoints=exported_endpoints,
        pricing_templates=exported_pricing_templates,
        models=exported_models,
        user_settings=ConfigUserSettingsExport(
            report_currency_code=(
                user_settings.report_currency_code
                if user_settings is not None
                else "USD"
            ),
            report_currency_symbol=(
                user_settings.report_currency_symbol
                if user_settings is not None
                else "$"
            ),
            endpoint_fx_mappings=[
                ConfigEndpointFxRateExport(
                    model_id=row.model_id,
                    endpoint_id=row.endpoint_id,
                    fx_rate=row.fx_rate,
                )
                for row in fx_mappings
            ],
        ),
        header_blocklist_rules=[
            HeaderBlocklistRuleExport(
                name=rule.name,
                match_type=rule.match_type,
                pattern=rule.pattern,
                enabled=rule.enabled,
            )
            for rule in header_blocklist_rules
        ],
    )

    date_str = utc_now().strftime("%Y-%m-%d")
    return JSONResponse(
        content=data.model_dump(mode="json"),
        headers={
            "Content-Disposition": f'attachment; filename="gateway-config-{date_str}.json"'
        },
    )


def _validate_import(data: ConfigImportRequest) -> None:
    endpoint_ids_in_file: set[int] = set()
    endpoint_names_in_file: set[str] = set()
    for endpoint in data.endpoints:
        if endpoint.endpoint_id in endpoint_ids_in_file:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate endpoint_id in import: {endpoint.endpoint_id}",
            )
        endpoint_ids_in_file.add(endpoint.endpoint_id)

        endpoint_name = endpoint.name.strip()
        if not endpoint_name:
            raise HTTPException(
                status_code=400, detail="Endpoint name must not be empty"
            )
        if endpoint_name in endpoint_names_in_file:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate endpoint name: '{endpoint_name}'",
            )
        endpoint_names_in_file.add(endpoint_name)

        normalized_url = normalize_base_url(endpoint.base_url)
        url_warnings = validate_base_url(normalized_url)
        if url_warnings:
            raise HTTPException(
                status_code=400,
                detail=f"Endpoint '{endpoint_name}' has invalid base_url: {'; '.join(url_warnings)}",
            )

        endpoint_position = endpoint.position
        if endpoint_position is not None and endpoint_position < 0:
            raise HTTPException(
                status_code=400,
                detail=f"Endpoint '{endpoint_name}' has invalid position '{endpoint_position}'",
            )
    pricing_template_ids_in_file: set[int] = set()
    pricing_template_names_in_file: set[str] = set()
    for template in data.pricing_templates:
        if template.pricing_template_id in pricing_template_ids_in_file:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Duplicate pricing_template_id in import: "
                    f"{template.pricing_template_id}"
                ),
            )
        pricing_template_ids_in_file.add(template.pricing_template_id)

        template_name = template.name.strip()
        if not template_name:
            raise HTTPException(
                status_code=400,
                detail="Pricing template name must not be empty",
            )
        if template_name in pricing_template_names_in_file:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate pricing template name: '{template_name}'",
            )
        pricing_template_names_in_file.add(template_name)
    seen_model_ids: set[str] = set()
    native_models: dict[str, str] = {}
    connection_ids_seen: set[int] = set()
    connection_pairs: set[tuple[str, int]] = set()

    for model in data.models:
        if model.model_id in seen_model_ids:
            raise HTTPException(
                status_code=400, detail=f"Duplicate model_id: '{model.model_id}'"
            )
        seen_model_ids.add(model.model_id)

        if model.provider_type not in VALID_PROVIDER_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown provider type: '{model.provider_type}'",
            )

        if model.model_type not in {"native", "proxy"}:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported model_type '{model.model_type}' for model '{model.model_id}'",
            )
        if model.model_type == "native":
            if model.redirect_to is not None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Native model '{model.model_id}' must not have redirect_to",
                )
            native_models[model.model_id] = model.provider_type
        else:
            if not model.redirect_to:
                raise HTTPException(
                    status_code=400,
                    detail=f"Proxy model '{model.model_id}' must include redirect_to",
                )
            if model.connections:
                raise HTTPException(
                    status_code=400,
                    detail=f"Proxy model '{model.model_id}' must not have connections",
                )

        for connection in model.connections:
            if connection.connection_id in connection_ids_seen:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Duplicate connection_id in import: {connection.connection_id}"
                    ),
                )
            connection_ids_seen.add(connection.connection_id)

            if connection.endpoint_id not in endpoint_ids_in_file:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Connection for model '{model.model_id}' references unknown "
                        f"endpoint_id '{connection.endpoint_id}'"
                    ),
                )

            connection_pairs.add((model.model_id, connection.endpoint_id))

            if (
                connection.pricing_template_id is not None
                and connection.pricing_template_id not in pricing_template_ids_in_file
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Connection for model '{model.model_id}' references unknown "
                        f"pricing_template_id '{connection.pricing_template_id}'"
                    ),
                )
    for model in data.models:
        if model.model_type != "proxy":
            continue
        if model.redirect_to not in native_models:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Model '{model.model_id}' references unknown redirect target "
                    f"'{model.redirect_to}'"
                ),
            )
        if native_models[model.redirect_to] != model.provider_type:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Model '{model.model_id}' cannot redirect cross-provider to "
                    f"'{model.redirect_to}'"
                ),
            )

    if data.user_settings is not None:
        seen_fx: set[tuple[str, int]] = set()
        for mapping in data.user_settings.endpoint_fx_mappings:
            key = (mapping.model_id, mapping.endpoint_id)
            if key in seen_fx:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Duplicate FX mapping in import for "
                        f"model_id='{mapping.model_id}', endpoint_id='{mapping.endpoint_id}'"
                    ),
                )
            seen_fx.add(key)
            if key not in connection_pairs:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "FX mapping must reference an imported model/endpoint_id connection pair: "
                        f"model_id='{mapping.model_id}', endpoint_id='{mapping.endpoint_id}'"
                    ),
                )

    seen_blocklist_rules: set[tuple[str, str]] = set()
    for rule in data.header_blocklist_rules:
        key = (rule.match_type, rule.pattern)
        if key in seen_blocklist_rules:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Duplicate header blocklist rule in import for "
                    f"match_type='{rule.match_type}', pattern='{rule.pattern}'"
                ),
            )
        seen_blocklist_rules.add(key)


@router.post("/import", response_model=ConfigImportResponse)
async def import_config(
    data: ConfigImportRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    _validate_import(data)
    if data.version != 2:
        raise HTTPException(status_code=400, detail="Config import requires version=2")

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
        normalized_url = normalize_base_url(endpoint_data.base_url)
        endpoint = Endpoint(
            profile_id=profile_id,
            name=endpoint_data.name.strip(),
            base_url=normalized_url,
            api_key=endpoint_data.api_key,
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
            missing_special_token_price_policy=template_data.missing_special_token_price_policy,
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
            provider_id=provider_map[model.provider_type],
            profile_id=profile_id,
            model_id=model.model_id,
            display_name=model.display_name,
            model_type=model.model_type,
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

        for connection_data in model.connections:
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
                model_config_id=model_config.id,
                profile_id=profile_id,
                endpoint_id=mapped_endpoint_id,
                pricing_template_id=(
                    template_id_map[connection_data.pricing_template_id]
                    if connection_data.pricing_template_id is not None
                    else None
                ),
                is_active=connection_data.is_active,
                priority=connection_data.priority,
                name=connection_data.name,
                auth_type=connection_data.auth_type,
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
                name=rule_data.name,
                profile_id=profile_id,
                match_type=rule_data.match_type,
                pattern=rule_data.pattern,
                enabled=rule_data.enabled,
                is_system=False,
            )
        )
    await db.flush()

    return ConfigImportResponse(
        endpoints_imported=endpoints_count,
        models_imported=len(data.models),
        pricing_templates_imported=templates_count,
        connections_imported=connections_count,
    )
