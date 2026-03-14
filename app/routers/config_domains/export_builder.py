import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.time import utc_now
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
from app.schemas.schemas import (
    ConfigConnectionExport,
    ConfigEndpointExport,
    ConfigEndpointFxRateExport,
    ConfigExportResponse,
    ConfigModelExport,
    ConfigPricingTemplateExport,
    ConfigUserSettingsExport,
    HeaderBlocklistRuleExport,
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

    exported_endpoints = [
        ConfigEndpointExport(
            endpoint_id=endpoint.id,
            name=endpoint.name,
            base_url=endpoint.base_url,
            api_key="",
            position=endpoint.position,
        )
        for endpoint in endpoints
    ]

    exported_pricing_templates = [
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
        for template in pricing_templates
    ]

    exported_models: list[ConfigModelExport] = []
    for model in model_configs:
        sorted_connections = sorted(
            model.connections,
            key=lambda c: (c.priority, c.id),
        )
        exported_connections = [
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
            for connection in sorted_connections
        ]
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

    return ConfigExportResponse(
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


__all__ = ["build_export_payload"]
