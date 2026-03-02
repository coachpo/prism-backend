from datetime import datetime, timezone
from typing import Annotated
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_db, get_effective_profile_id
from app.models.models import (
    Connection,
    Provider,
    ModelConfig,
    Endpoint,
    HeaderBlocklistRule,
    UserSetting,
    EndpointFxRateSetting,
)
from app.schemas.schemas import (
    ConfigExportResponse,
    ConfigModelExport,
    ConfigConnectionExport,
    ConfigEndpointExport,
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

router = APIRouter(prefix="/api/config", tags=["config"])

VALID_PROVIDER_TYPES = {"openai", "anthropic", "gemini"}

def _normalize_optional_ref(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _resolve_endpoint_source_ref(*, endpoint_ref: str | None) -> str | None:
    return _normalize_optional_ref(endpoint_ref)


def _resolve_connection_source_ref(*, connection_ref: str | None) -> str | None:
    return _normalize_optional_ref(connection_ref)


def _build_export_endpoint_ref(endpoint: Endpoint) -> str:
    endpoint_name = endpoint.name.strip() or str(endpoint.id)
    return f"endpoint:{endpoint_name}"


def _build_export_connection_ref(
    *,
    model_id: str,
    endpoint_ref: str,
    connection: Connection,
    ordinal: int,
 ) -> str:
    connection_name = (connection.name or "").strip() or "unnamed"
    return (
        f"connection:{model_id}:{endpoint_ref}:{connection.priority}:"
        f"{connection_name}:{ordinal}"
    )


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
                .order_by(Endpoint.id.asc())
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
                    )
                )
                .where(ModelConfig.profile_id == profile_id)
                .order_by(ModelConfig.id.asc())
            )
        )
        .scalars()
        .all()
    )

    provider_ids = {model.provider_id for model in model_configs}
    provider_type_map: dict[int, str] = {}
    if provider_ids:
        providers = (
            (
                await db.execute(select(Provider).where(Provider.id.in_(provider_ids)))
            )
            .scalars()
            .all()
        )
        provider_type_map = {provider.id: provider.provider_type for provider in providers}

    endpoint_ref_by_id: dict[int, str] = {}
    exported_endpoints: list[ConfigEndpointExport] = []
    for endpoint in endpoints:
        endpoint_ref = _build_export_endpoint_ref(endpoint)
        endpoint_ref_by_id[endpoint.id] = endpoint_ref
        exported_endpoints.append(
            ConfigEndpointExport(
                endpoint_ref=endpoint_ref,
                name=endpoint.name,
                base_url=endpoint.base_url,
                api_key=endpoint.api_key,
            )
        )

    exported_models: list[ConfigModelExport] = []
    for model in model_configs:
        sorted_connections = sorted(
            model.connections,
            key=lambda c: (c.priority, c.id),
        )
        exported_connections: list[ConfigConnectionExport] = []
        for ordinal, connection in enumerate(sorted_connections):
            endpoint_ref = endpoint_ref_by_id[connection.endpoint_id]
            exported_connections.append(
                ConfigConnectionExport(
                    connection_ref=_build_export_connection_ref(
                        model_id=model.model_id,
                        endpoint_ref=endpoint_ref,
                        connection=connection,
                        ordinal=ordinal,
                    ),
                    endpoint_ref=endpoint_ref,
                    is_active=connection.is_active,
                    priority=connection.priority,
                    name=connection.name,
                    auth_type=connection.auth_type,
                    custom_headers=json.loads(connection.custom_headers)
                    if connection.custom_headers is not None
                    else None,
                    pricing_enabled=connection.pricing_enabled,
                    pricing_currency_code=connection.pricing_currency_code,
                    input_price=connection.input_price,
                    output_price=connection.output_price,
                    cached_input_price=connection.cached_input_price,
                    cache_creation_price=connection.cache_creation_price,
                    reasoning_price=connection.reasoning_price,
                    missing_special_token_price_policy=(
                        "ZERO_COST"
                        if connection.missing_special_token_price_policy == "ZERO_COST"
                        else "MAP_TO_OUTPUT"
                    ),
                    pricing_config_version=connection.pricing_config_version,
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
                        select(HeaderBlocklistRule).where(
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
        mode="replace",
        exported_at=datetime.now(timezone.utc),
        endpoints=exported_endpoints,
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
                    endpoint_ref=endpoint_ref_by_id[row.endpoint_id],
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

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return JSONResponse(
        content=data.model_dump(mode="json"),
        headers={
            "Content-Disposition": f'attachment; filename="gateway-config-{date_str}.json"'
        },
    )


def _validate_import(data: ConfigImportRequest) -> None:
    endpoint_refs_in_file: set[str] = set()
    endpoint_names_in_file: set[str] = set()
    for endpoint in data.endpoints:
        endpoint_source_ref = _resolve_endpoint_source_ref(
            endpoint_ref=endpoint.endpoint_ref,
        )
        if endpoint_source_ref is None:
            raise HTTPException(
                status_code=400,
                detail="Each endpoint must include endpoint_ref",
            )
        if endpoint_source_ref in endpoint_refs_in_file:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate endpoint reference: '{endpoint_source_ref}'",
            )
        endpoint_refs_in_file.add(endpoint_source_ref)

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

    seen_model_ids: set[str] = set()
    native_models: dict[str, str] = {}
    connection_ref_seen: set[str] = set()
    connection_pairs: set[tuple[str, str]] = set()

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

        if model.model_type == "native":
            if model.redirect_to is not None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Native model '{model.model_id}' must not have redirect_to",
                )
            native_models[model.model_id] = model.provider_type
        elif model.model_type == "proxy":
            if model.connections:
                raise HTTPException(
                    status_code=400,
                    detail=f"Proxy model '{model.model_id}' must not have connections",
                )

        for connection in model.connections:
            endpoint_source_ref = _resolve_endpoint_source_ref(
                endpoint_ref=connection.endpoint_ref,
            )
            if endpoint_source_ref is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Connection for model '{model.model_id}' must include endpoint_ref"
                    ),
                )
            if endpoint_source_ref not in endpoint_refs_in_file:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Connection for model '{model.model_id}' references unknown "
                        f"endpoint reference '{endpoint_source_ref}'"
                    ),
                )

            connection_source_ref = _resolve_connection_source_ref(
                connection_ref=connection.connection_ref,
            )
            if connection_source_ref is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Connection for model '{model.model_id}' must include connection_ref"
                    ),
                )
            if connection_source_ref in connection_ref_seen:
                raise HTTPException(
                    status_code=400,
                    detail=f"Duplicate connection reference: '{connection_source_ref}'",
                )
            connection_ref_seen.add(connection_source_ref)

            connection_pairs.add((model.model_id, endpoint_source_ref))

            if connection.pricing_enabled and connection.pricing_currency_code is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Connection for model '{model.model_id}' has pricing_enabled=true "
                        "but pricing_currency_code is missing"
                    ),
                )

    for model in data.models:
        if model.model_type == "proxy":
            if not model.redirect_to or model.redirect_to not in native_models:
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
        seen_fx: set[tuple[str, str]] = set()
        for mapping in data.user_settings.endpoint_fx_mappings:
            endpoint_source_ref = _resolve_endpoint_source_ref(
                endpoint_ref=mapping.endpoint_ref,
            )
            if endpoint_source_ref is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "FX mapping must include endpoint_ref: "
                        f"model_id='{mapping.model_id}'"
                    ),
                )
            key = (mapping.model_id, endpoint_source_ref)
            if key in seen_fx:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Duplicate FX mapping in import for "
                        f"model_id='{mapping.model_id}', endpoint_ref='{endpoint_source_ref}'"
                    ),
                )
            seen_fx.add(key)
            if key not in connection_pairs:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "FX mapping must reference an imported model/endpoint connection pair: "
                        f"model_id='{mapping.model_id}', endpoint_ref='{endpoint_source_ref}'"
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

    await db.execute(
        delete(EndpointFxRateSetting).where(
            EndpointFxRateSetting.profile_id == profile_id
        )
    )
    await db.execute(delete(Connection).where(Connection.profile_id == profile_id))
    await db.execute(delete(Endpoint).where(Endpoint.profile_id == profile_id))
    await db.execute(delete(ModelConfig).where(ModelConfig.profile_id == profile_id))
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
                    select(Provider).where(Provider.provider_type.in_(provider_types_needed))
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

    endpoint_ref_map: dict[str, int] = {}
    endpoints_count = 0
    for endpoint_data in data.endpoints:
        endpoint_source_ref = _resolve_endpoint_source_ref(
            endpoint_ref=endpoint_data.endpoint_ref,
        )
        if endpoint_source_ref is None:
            raise HTTPException(
                status_code=400,
                detail="Each endpoint must include endpoint_ref",
            )

        normalized_url = normalize_base_url(endpoint_data.base_url)
        endpoint = Endpoint(
            profile_id=profile_id,
            name=endpoint_data.name.strip(),
            base_url=normalized_url,
            api_key=endpoint_data.api_key,
        )
        db.add(endpoint)
        await db.flush()

        endpoint_ref_map[endpoint_source_ref] = endpoint.id
        endpoints_count += 1

    connections_count = 0
    imported_connection_pairs: set[tuple[str, str]] = set()

    native_models = [model for model in data.models if model.model_type == "native"]
    proxy_models = [model for model in data.models if model.model_type == "proxy"]

    for model in native_models:
        model_config = ModelConfig(
            provider_id=provider_map[model.provider_type],
            profile_id=profile_id,
            model_id=model.model_id,
            display_name=model.display_name,
            model_type=model.model_type,
            redirect_to=model.redirect_to,
            lb_strategy=model.lb_strategy,
            failover_recovery_enabled=model.failover_recovery_enabled,
            failover_recovery_cooldown_seconds=model.failover_recovery_cooldown_seconds,
            is_enabled=model.is_enabled,
        )
        db.add(model_config)
        await db.flush()

        for connection_data in model.connections:
            endpoint_source_ref = _resolve_endpoint_source_ref(
                endpoint_ref=connection_data.endpoint_ref,
            )
            if endpoint_source_ref is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Connection for model '{model.model_id}' must include endpoint_ref"
                    ),
                )
            mapped_endpoint_id = endpoint_ref_map.get(endpoint_source_ref)
            if mapped_endpoint_id is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Connection for model '{model.model_id}' references unknown "
                        f"endpoint reference '{endpoint_source_ref}'"
                    ),
                )

            connection = Connection(
                model_config_id=model_config.id,
                profile_id=profile_id,
                endpoint_id=mapped_endpoint_id,
                is_active=connection_data.is_active,
                priority=connection_data.priority,
                name=connection_data.name,
                auth_type=connection_data.auth_type,
                custom_headers=json.dumps(connection_data.custom_headers)
                if connection_data.custom_headers is not None
                else None,
                pricing_enabled=connection_data.pricing_enabled,
                pricing_currency_code=connection_data.pricing_currency_code,
                input_price=connection_data.input_price,
                output_price=connection_data.output_price,
                cached_input_price=connection_data.cached_input_price,
                cache_creation_price=connection_data.cache_creation_price,
                reasoning_price=connection_data.reasoning_price,
                missing_special_token_price_policy=connection_data.missing_special_token_price_policy,
                pricing_config_version=connection_data.pricing_config_version,
            )
            db.add(connection)
            connections_count += 1
            imported_connection_pairs.add((model.model_id, endpoint_source_ref))

    for model in proxy_models:
        model_config = ModelConfig(
            provider_id=provider_map[model.provider_type],
            profile_id=profile_id,
            model_id=model.model_id,
            display_name=model.display_name,
            model_type=model.model_type,
            redirect_to=model.redirect_to,
            lb_strategy=model.lb_strategy,
            failover_recovery_enabled=model.failover_recovery_enabled,
            failover_recovery_cooldown_seconds=model.failover_recovery_cooldown_seconds,
            is_enabled=model.is_enabled,
        )
        db.add(model_config)

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
            endpoint_source_ref = _resolve_endpoint_source_ref(
                endpoint_ref=mapping.endpoint_ref,
            )
            if endpoint_source_ref is None:
                continue
            if (mapping.model_id, endpoint_source_ref) not in imported_connection_pairs:
                continue
            mapped_endpoint_id = endpoint_ref_map.get(endpoint_source_ref)
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
        connections_imported=connections_count,
    )


@router.get(
    "/header-blocklist-rules",
    response_model=list[HeaderBlocklistRuleResponse],
)
async def list_header_blocklist_rules(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    include_disabled: bool = True,
):
    query = select(HeaderBlocklistRule).where(
        (HeaderBlocklistRule.is_system == True)  # noqa: E712
        | (HeaderBlocklistRule.profile_id == profile_id)
    ).order_by(
        HeaderBlocklistRule.is_system.desc(),
        HeaderBlocklistRule.id.asc(),
    )
    if not include_disabled:
        query = query.where(HeaderBlocklistRule.enabled == True)  # noqa: E712
    return (await db.execute(query)).scalars().all()


@router.get(
    "/header-blocklist-rules/{rule_id}",
    response_model=HeaderBlocklistRuleResponse,
)
async def get_header_blocklist_rule(
    rule_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    rule = (
        await db.execute(
            select(HeaderBlocklistRule).where(
                HeaderBlocklistRule.id == rule_id,
                (HeaderBlocklistRule.is_system == True)  # noqa: E712
                | (HeaderBlocklistRule.profile_id == profile_id),
            )
        )
    ).scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Header blocklist rule not found")
    return rule


@router.post(
    "/header-blocklist-rules",
    response_model=HeaderBlocklistRuleResponse,
    status_code=201,
)
async def create_header_blocklist_rule(
    body: HeaderBlocklistRuleCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    existing = (
        await db.execute(
            select(HeaderBlocklistRule).where(
                HeaderBlocklistRule.match_type == body.match_type,
                HeaderBlocklistRule.pattern == body.pattern,
                (HeaderBlocklistRule.is_system == True)  # noqa: E712
                | (HeaderBlocklistRule.profile_id == profile_id),
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Rule with match_type='{body.match_type}' and pattern='{body.pattern}' already exists",
        )

    rule = HeaderBlocklistRule(
        name=body.name,
        match_type=body.match_type,
        profile_id=profile_id,
        pattern=body.pattern,
        enabled=body.enabled,
        is_system=False,
    )
    db.add(rule)
    await db.flush()
    await db.refresh(rule)
    return rule


@router.patch(
    "/header-blocklist-rules/{rule_id}",
    response_model=HeaderBlocklistRuleResponse,
)
async def update_header_blocklist_rule(
    rule_id: int,
    body: HeaderBlocklistRuleUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    rule = (
        await db.execute(
            select(HeaderBlocklistRule).where(
                HeaderBlocklistRule.id == rule_id,
                (HeaderBlocklistRule.is_system == True)  # noqa: E712
                | (HeaderBlocklistRule.profile_id == profile_id),
            )
        )
    ).scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Header blocklist rule not found")

    update_data = body.model_dump(exclude_unset=True)

    if rule.is_system:
        immutable_fields = {"name", "match_type", "pattern"}
        attempted = immutable_fields & set(update_data.keys())
        if attempted:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot modify {', '.join(sorted(attempted))} on a system rule. Only 'enabled' is mutable.",
            )

    if "match_type" in update_data or "pattern" in update_data:
        new_match_type = update_data.get("match_type", rule.match_type)
        new_pattern = update_data.get("pattern", rule.pattern)
        if new_match_type == "prefix" and not new_pattern.endswith("-"):
            raise HTTPException(
                status_code=400,
                detail="prefix pattern must end with '-'",
            )
        existing = (
            await db.execute(
                select(HeaderBlocklistRule).where(
                    HeaderBlocklistRule.match_type == new_match_type,
                    HeaderBlocklistRule.pattern == new_pattern,
                    HeaderBlocklistRule.id != rule_id,
                    (HeaderBlocklistRule.is_system == True)  # noqa: E712
                    | (HeaderBlocklistRule.profile_id == profile_id),
                )
            )
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Rule with match_type='{new_match_type}' and pattern='{new_pattern}' already exists",
            )

    for field, value in update_data.items():
        setattr(rule, field, value)
    rule.updated_at = datetime.now(timezone.utc)

    await db.flush()
    await db.refresh(rule)
    return rule


@router.delete(
    "/header-blocklist-rules/{rule_id}",
    status_code=204,
)
async def delete_header_blocklist_rule(
    rule_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    rule = (
        await db.execute(
            select(HeaderBlocklistRule).where(
                HeaderBlocklistRule.id == rule_id,
                HeaderBlocklistRule.profile_id == profile_id,
            )
        )
    ).scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Header blocklist rule not found")
    if rule.is_system:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete a system rule. Disable it instead.",
        )
    await db.delete(rule)
    await db.flush()
