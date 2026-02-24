from datetime import datetime, timezone
from typing import Annotated, Literal, cast
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_db
from app.models.models import (
    Provider,
    ModelConfig,
    Endpoint,
    HeaderBlocklistRule,
    UserSetting,
    EndpointFxRateSetting,
)
from app.schemas.schemas import (
    ConfigExportResponse,
    ConfigProviderExport,
    ConfigModelExport,
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])

VALID_PROVIDER_TYPES = {"openai", "anthropic", "gemini"}


@router.get("/export")
async def export_config(db: Annotated[AsyncSession, Depends(get_db)]):
    providers = (await db.execute(select(Provider))).scalars().all()
    models_q = select(ModelConfig).options(selectinload(ModelConfig.endpoints))
    model_configs = (await db.execute(models_q)).scalars().all()

    provider_type_map = {p.id: p.provider_type for p in providers}

    exported_providers = [
        ConfigProviderExport(
            name=p.name,
            provider_type=p.provider_type,
            description=p.description,
            audit_enabled=p.audit_enabled,
            audit_capture_bodies=p.audit_capture_bodies,
        )
        for p in providers
    ]

    exported_models = [
        ConfigModelExport(
            provider_type=provider_type_map.get(mc.provider_id, ""),
            model_id=mc.model_id,
            display_name=mc.display_name,
            model_type=mc.model_type,
            redirect_to=mc.redirect_to,
            lb_strategy="failover" if mc.lb_strategy == "failover" else "single",
            failover_recovery_enabled=mc.failover_recovery_enabled,
            failover_recovery_cooldown_seconds=mc.failover_recovery_cooldown_seconds,
            is_enabled=mc.is_enabled,
            endpoints=[
                ConfigEndpointExport(
                    endpoint_id=ep.id,
                    base_url=ep.base_url,
                    api_key=ep.api_key,
                    is_active=ep.is_active,
                    priority=ep.priority,
                    description=ep.description,
                    auth_type=ep.auth_type,
                    custom_headers=json.loads(ep.custom_headers)
                    if ep.custom_headers is not None
                    else None,
                    pricing_enabled=ep.pricing_enabled,
                    pricing_unit=cast(
                        Literal["PER_1K", "PER_1M"] | None,
                        ep.pricing_unit
                        if ep.pricing_unit in ("PER_1K", "PER_1M")
                        else None,
                    ),
                    pricing_currency_code=ep.pricing_currency_code,
                    input_price=ep.input_price,
                    output_price=ep.output_price,
                    cached_input_price=ep.cached_input_price,
                    reasoning_price=ep.reasoning_price,
                    missing_special_token_policy=cast(
                        Literal["MAP_TO_OUTPUT", "ZERO_COST"],
                        "ZERO_COST"
                        if ep.missing_special_token_policy == "ZERO_COST"
                        else "MAP_TO_OUTPUT",
                    ),
                    pricing_config_version=ep.pricing_config_version,
                )
                for ep in mc.endpoints
            ],
        )
        for mc in model_configs
    ]

    user_settings = (
        await db.execute(select(UserSetting).order_by(UserSetting.id.asc()).limit(1))
    ).scalar_one_or_none()
    fx_mappings = (
        (
            await db.execute(
                select(EndpointFxRateSetting).order_by(
                    EndpointFxRateSetting.model_id.asc(),
                    EndpointFxRateSetting.endpoint_id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )

    data = ConfigExportResponse(
        version=3,
        exported_at=datetime.now(timezone.utc),
        providers=exported_providers,
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
                name=r.name,
                match_type=r.match_type,
                pattern=r.pattern,
                enabled=r.enabled,
                is_system=r.is_system,
            )
            for r in (
                await db.execute(
                    select(HeaderBlocklistRule).order_by(
                        HeaderBlocklistRule.is_system.desc(),
                        HeaderBlocklistRule.id.asc(),
                    )
                )
            )
            .scalars()
            .all()
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
    if data.version not in (2, 3):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported config version: {data.version}. Expected: 2 or 3",
        )

    if not data.providers:
        raise HTTPException(status_code=400, detail="At least one provider is required")

    seen_provider_types: set[str] = set()
    for p in data.providers:
        if p.provider_type not in VALID_PROVIDER_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown provider type: '{p.provider_type}'",
            )
        if p.provider_type in seen_provider_types:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate provider type: '{p.provider_type}'",
            )
        seen_provider_types.add(p.provider_type)

    provider_types_in_file = {p.provider_type for p in data.providers}
    seen_model_ids: set[str] = set()
    native_models: dict[str, str] = {}

    for m in data.models:
        if m.model_id in seen_model_ids:
            raise HTTPException(
                status_code=400, detail=f"Duplicate model_id: '{m.model_id}'"
            )
        seen_model_ids.add(m.model_id)

        if m.provider_type not in provider_types_in_file:
            raise HTTPException(
                status_code=400,
                detail=f"Model '{m.model_id}' references unknown provider type '{m.provider_type}'",
            )

        if m.model_type == "native":
            if m.redirect_to is not None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Native model '{m.model_id}' must not have redirect_to",
                )
            native_models[m.model_id] = m.provider_type
        elif m.model_type == "proxy":
            if m.endpoints:
                raise HTTPException(
                    status_code=400,
                    detail=f"Proxy model '{m.model_id}' must not have endpoints",
                )

        for endpoint in m.endpoints:
            if endpoint.pricing_enabled:
                if endpoint.pricing_unit is None:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Endpoint for model '{m.model_id}' has pricing_enabled=true "
                            "but pricing_unit is missing"
                        ),
                    )
                if endpoint.pricing_currency_code is None:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Endpoint for model '{m.model_id}' has pricing_enabled=true "
                            "but pricing_currency_code is missing"
                        ),
                    )

    for m in data.models:
        if m.model_type == "proxy":
            if not m.redirect_to or m.redirect_to not in native_models:
                raise HTTPException(
                    status_code=400,
                    detail=f"Model '{m.model_id}' references unknown redirect target '{m.redirect_to}'",
                )
            if native_models[m.redirect_to] != m.provider_type:
                raise HTTPException(
                    status_code=400,
                    detail=f"Model '{m.model_id}' cannot redirect cross-provider to '{m.redirect_to}'",
                )
        if m.lb_strategy == "round_robin":
            raise HTTPException(
                status_code=400,
                detail=f"Model '{m.model_id}' uses unsupported lb_strategy 'round_robin'. Use 'single' or 'failover'.",
            )

    if data.version == 3 and data.user_settings is not None:
        seen_fx: set[tuple[str, int]] = set()
        for mapping in data.user_settings.endpoint_fx_mappings:
            key = (mapping.model_id, mapping.endpoint_id)
            if key in seen_fx:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Duplicate FX mapping in import for "
                        f"model_id='{mapping.model_id}', endpoint_id={mapping.endpoint_id}"
                    ),
                )
            seen_fx.add(key)


@router.post("/import", response_model=ConfigImportResponse)
async def import_config(
    data: ConfigImportRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _validate_import(data)

    await db.execute(delete(EndpointFxRateSetting))
    await db.execute(delete(Endpoint))
    await db.execute(delete(ModelConfig))
    await db.execute(delete(Provider))
    await db.flush()

    provider_map: dict[str, int] = {}
    for p in data.providers:
        provider = Provider(
            name=p.name,
            provider_type=p.provider_type,
            description=p.description,
            audit_enabled=p.audit_enabled,
            audit_capture_bodies=p.audit_capture_bodies,
        )
        db.add(provider)
        await db.flush()
        provider_map[p.provider_type] = provider.id

    endpoints_count = 0

    native_models = [m for m in data.models if m.model_type == "native"]
    proxy_models = [m for m in data.models if m.model_type == "proxy"]

    for m in native_models:
        mc = ModelConfig(
            provider_id=provider_map[m.provider_type],
            model_id=m.model_id,
            display_name=m.display_name,
            model_type=m.model_type,
            redirect_to=m.redirect_to,
            lb_strategy=m.lb_strategy,
            failover_recovery_enabled=m.failover_recovery_enabled,
            failover_recovery_cooldown_seconds=m.failover_recovery_cooldown_seconds,
            is_enabled=m.is_enabled,
        )
        db.add(mc)
        await db.flush()

        for ep_data in m.endpoints:
            ep = Endpoint(
                id=ep_data.endpoint_id,
                model_config_id=mc.id,
                base_url=ep_data.base_url,
                api_key=ep_data.api_key,
                is_active=ep_data.is_active,
                priority=ep_data.priority,
                description=ep_data.description,
                auth_type=ep_data.auth_type,
                custom_headers=json.dumps(ep_data.custom_headers)
                if ep_data.custom_headers is not None
                else None,
                pricing_enabled=ep_data.pricing_enabled,
                pricing_unit=ep_data.pricing_unit,
                pricing_currency_code=ep_data.pricing_currency_code,
                input_price=ep_data.input_price,
                output_price=ep_data.output_price,
                cached_input_price=ep_data.cached_input_price,
                reasoning_price=ep_data.reasoning_price,
                missing_special_token_policy=ep_data.missing_special_token_policy,
                pricing_config_version=ep_data.pricing_config_version,
            )
            db.add(ep)
            endpoints_count += 1

    for m in proxy_models:
        mc = ModelConfig(
            provider_id=provider_map[m.provider_type],
            model_id=m.model_id,
            display_name=m.display_name,
            model_type=m.model_type,
            redirect_to=m.redirect_to,
            lb_strategy=m.lb_strategy,
            failover_recovery_enabled=m.failover_recovery_enabled,
            failover_recovery_cooldown_seconds=m.failover_recovery_cooldown_seconds,
            is_enabled=m.is_enabled,
        )
        db.add(mc)

    await db.flush()

    user_settings = (
        await db.execute(select(UserSetting).order_by(UserSetting.id.asc()).limit(1))
    ).scalar_one_or_none()
    if user_settings is None:
        user_settings = UserSetting(
            report_currency_code="USD", report_currency_symbol="$"
        )
        db.add(user_settings)

    if data.version == 3 and data.user_settings is not None:
        user_settings.report_currency_code = data.user_settings.report_currency_code
        user_settings.report_currency_symbol = data.user_settings.report_currency_symbol

        endpoint_rows = (
            (
                await db.execute(
                    select(Endpoint)
                    .options(selectinload(Endpoint.model_config_rel))
                    .where(
                        Endpoint.id.in_(
                            [
                                m.endpoint_id
                                for m in data.user_settings.endpoint_fx_mappings
                            ]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        endpoint_model_map = {
            ep.id: ep.model_config_rel.model_id
            for ep in endpoint_rows
            if ep.model_config_rel is not None
        }

        for mapping in data.user_settings.endpoint_fx_mappings:
            endpoint_model_id = endpoint_model_map.get(mapping.endpoint_id)
            if endpoint_model_id is None:
                continue
            if endpoint_model_id != mapping.model_id:
                continue
            db.add(
                EndpointFxRateSetting(
                    model_id=mapping.model_id,
                    endpoint_id=mapping.endpoint_id,
                    fx_rate=mapping.fx_rate,
                )
            )
    else:
        user_settings.report_currency_code = "USD"
        user_settings.report_currency_symbol = "$"

    if data.header_blocklist_rules is not None:
        from app.main import SYSTEM_BLOCKLIST_DEFAULTS

        system_patterns = {
            (d["match_type"], d["pattern"]) for d in SYSTEM_BLOCKLIST_DEFAULTS
        }

        for rule_data in data.header_blocklist_rules:
            if rule_data.is_system:
                if (rule_data.match_type, rule_data.pattern) not in system_patterns:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Unknown system blocklist rule: ({rule_data.match_type}, {rule_data.pattern})",
                    )

        await db.execute(
            delete(HeaderBlocklistRule).where(
                HeaderBlocklistRule.is_system == False  # noqa: E712
            )
        )
        await db.flush()

        for rule_data in data.header_blocklist_rules:
            if rule_data.is_system:
                existing = (
                    await db.execute(
                        select(HeaderBlocklistRule).where(
                            HeaderBlocklistRule.match_type == rule_data.match_type,
                            HeaderBlocklistRule.pattern == rule_data.pattern,
                            HeaderBlocklistRule.is_system == True,  # noqa: E712
                        )
                    )
                ).scalar_one_or_none()
                if existing:
                    existing.enabled = rule_data.enabled
                    existing.updated_at = datetime.now(timezone.utc)
            else:
                db.add(
                    HeaderBlocklistRule(
                        name=rule_data.name,
                        match_type=rule_data.match_type,
                        pattern=rule_data.pattern,
                        enabled=rule_data.enabled,
                        is_system=False,
                    )
                )
        await db.flush()

    return ConfigImportResponse(
        providers_imported=len(data.providers),
        models_imported=len(data.models),
        endpoints_imported=endpoints_count,
    )


@router.get(
    "/header-blocklist-rules",
    response_model=list[HeaderBlocklistRuleResponse],
)
async def list_header_blocklist_rules(
    db: Annotated[AsyncSession, Depends(get_db)],
    include_disabled: bool = True,
):
    query = select(HeaderBlocklistRule).order_by(
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
):
    rule = await db.get(HeaderBlocklistRule, rule_id)
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
):
    existing = (
        await db.execute(
            select(HeaderBlocklistRule).where(
                HeaderBlocklistRule.match_type == body.match_type,
                HeaderBlocklistRule.pattern == body.pattern,
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
):
    rule = await db.get(HeaderBlocklistRule, rule_id)
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
):
    rule = await db.get(HeaderBlocklistRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Header blocklist rule not found")
    if rule.is_system:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete a system rule. Disable it instead.",
        )
    await db.delete(rule)
    await db.flush()
