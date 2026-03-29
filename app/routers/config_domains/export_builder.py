import json
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.crypto import decrypt_secret
from app.core.time import utc_now
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
from app.schemas.schemas import (
    ConfigConnectionExport,
    ConfigEndpointExport,
    ConfigEndpointFxRateExport,
    ConfigExportResponse,
    ConfigLoadbalanceStrategyExport,
    ConfigModelExport,
    ConfigProxyTargetExport,
    ConfigPricingTemplateExport,
    ConfigUserSettingsExport,
    ConfigVendorExport,
    HeaderBlocklistRuleExport,
)
from app.services.loadbalancer.policy import (
    resolve_effective_loadbalance_policy,
    serialize_auto_recovery,
)


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


def _export_endpoint_api_key(api_key: str | None) -> str:
    try:
        return decrypt_secret(api_key)
    except ValueError:
        return ""


async def build_export_payload(
    db: AsyncSession, *, profile_id: int
) -> ConfigExportResponse:
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
                    selectinload(ModelConfig.loadbalance_strategy),
                    selectinload(ModelConfig.proxy_targets).selectinload(
                        ModelProxyTarget.target_model_config
                    ),
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
    loadbalance_strategies = (
        (
            await db.execute(
                select(LoadbalanceStrategy)
                .where(LoadbalanceStrategy.profile_id == profile_id)
                .order_by(LoadbalanceStrategy.name.asc(), LoadbalanceStrategy.id.asc())
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

    vendor_ids = {model.vendor_id for model in model_configs}
    vendors_by_id: dict[int, Vendor] = {}
    if vendor_ids:
        vendors = (
            (await db.execute(select(Vendor).where(Vendor.id.in_(vendor_ids))))
            .scalars()
            .all()
        )
        vendors_by_id = {vendor.id: vendor for vendor in vendors}

    exported_endpoints = [
        ConfigEndpointExport(
            name=endpoint.name,
            base_url=endpoint.base_url,
            api_key=_export_endpoint_api_key(endpoint.api_key),
            position=endpoint.position,
        )
        for endpoint in endpoints
    ]

    endpoint_name_by_id = {endpoint.id: endpoint.name for endpoint in endpoints}

    exported_pricing_templates = [
        ConfigPricingTemplateExport(
            name=template.name,
            description=template.description,
            pricing_unit="PER_1M",
            pricing_currency_code=template.pricing_currency_code,
            input_price=template.input_price,
            output_price=template.output_price,
            cached_input_price=template.cached_input_price,
            cache_creation_price=template.cache_creation_price,
            reasoning_price=template.reasoning_price,
            missing_special_token_price_policy=cast(
                Literal["MAP_TO_OUTPUT", "ZERO_COST"],
                template.missing_special_token_price_policy,
            ),
            version=template.version,
        )
        for template in pricing_templates
    ]

    exported_loadbalance_strategies = []
    for strategy in loadbalance_strategies:
        exported_loadbalance_strategies.append(
            ConfigLoadbalanceStrategyExport.model_validate(
                {
                    "name": strategy.name,
                    "strategy_type": strategy.strategy_type,
                    "auto_recovery": strategy.auto_recovery,
                }
            )
        )

    pricing_template_name_by_id = {
        template.id: template.name for template in pricing_templates
    }

    exported_vendors = [
        ConfigVendorExport(
            key=vendor.key,
            name=vendor.name,
            description=vendor.description,
            icon_key=vendor.icon_key,
            audit_enabled=vendor.audit_enabled,
            audit_capture_bodies=vendor.audit_capture_bodies,
        )
        for vendor in sorted(vendors_by_id.values(), key=lambda vendor: vendor.key)
    ]

    exported_models: list[ConfigModelExport] = []
    for model in model_configs:
        sorted_connections = sorted(
            model.connections,
            key=lambda c: (c.priority, c.id),
        )
        exported_connections: list[ConfigConnectionExport] = []
        for connection in sorted_connections:
            endpoint_name = (
                connection.endpoint_rel.name
                if connection.endpoint_rel is not None
                else endpoint_name_by_id.get(connection.endpoint_id)
            )
            if endpoint_name is None:
                raise ValueError(
                    "Connection references endpoint missing from export payload"
                )

            pricing_template_name: str | None = None
            if connection.pricing_template_id is not None:
                pricing_template_name = (
                    connection.pricing_template_rel.name
                    if connection.pricing_template_rel is not None
                    else pricing_template_name_by_id.get(connection.pricing_template_id)
                )
                if pricing_template_name is None:
                    raise ValueError(
                        "Connection references pricing template missing from export payload"
                    )

            exported_connections.append(
                ConfigConnectionExport(
                    endpoint_name=endpoint_name,
                    pricing_template_name=pricing_template_name,
                    is_active=connection.is_active,
                    priority=connection.priority,
                    name=connection.name,
                    auth_type=cast(
                        Literal["openai", "anthropic", "gemini"] | None,
                        connection.auth_type,
                    ),
                    custom_headers=_normalize_custom_headers_for_export(
                        connection.custom_headers
                    ),
                    qps_limit=connection.qps_limit,
                    max_in_flight_non_stream=connection.max_in_flight_non_stream,
                    max_in_flight_stream=connection.max_in_flight_stream,
                )
            )
        exported_models.append(
            ConfigModelExport(
                vendor_key=vendors_by_id[model.vendor_id].key,
                api_family=cast(
                    Literal["openai", "anthropic", "gemini"], model.api_family
                ),
                model_id=model.model_id,
                display_name=model.display_name,
                model_type=cast(Literal["native", "proxy"], model.model_type),
                proxy_targets=[
                    ConfigProxyTargetExport(
                        target_model_id=cast(str, proxy_target.target_model_id),
                        position=proxy_target.position,
                    )
                    for proxy_target in sorted(
                        model.proxy_targets,
                        key=lambda proxy_target: proxy_target.position,
                    )
                ],
                loadbalance_strategy_name=(
                    model.loadbalance_strategy.name
                    if model.model_type == "native"
                    and model.loadbalance_strategy is not None
                    else None
                ),
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

    return ConfigExportResponse(
        version=1,
        exported_at=utc_now(),
        vendors=exported_vendors,
        endpoints=exported_endpoints,
        pricing_templates=exported_pricing_templates,
        loadbalance_strategies=exported_loadbalance_strategies,
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
                    endpoint_name=endpoint_name_by_id[row.endpoint_id],
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


__all__ = ["build_export_payload"]
