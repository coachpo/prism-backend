from fastapi import HTTPException

from app.schemas.schemas import ConfigImportRequest
from app.services.proxy_service import normalize_base_url, validate_base_url

VALID_API_FAMILIES = {"openai", "anthropic", "gemini"}


def _validate_optional_connection_limiter_fields(*, model_id: str, connection) -> None:
    limiter_fields = {
        "qps_limit": connection.qps_limit,
        "max_in_flight_non_stream": connection.max_in_flight_non_stream,
        "max_in_flight_stream": connection.max_in_flight_stream,
    }
    for field_name, limiter_value in limiter_fields.items():
        if limiter_value is not None and limiter_value < 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Connection for model '{model_id}' has invalid {field_name} "
                    f"'{limiter_value}'"
                ),
            )


def _normalize_reference_name(*, field: str, value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail=f"{field} must not be empty")
    return normalized


def _resolve_endpoint_reference_name(
    *,
    context: str,
    endpoint_name: str | None,
    endpoint_names_in_file: set[str],
) -> str:
    resolved_endpoint_name = _normalize_reference_name(
        field="endpoint_name", value=endpoint_name
    )
    if resolved_endpoint_name is None:
        raise HTTPException(
            status_code=400,
            detail=f"{context} must include endpoint_name",
        )

    if resolved_endpoint_name not in endpoint_names_in_file:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{context} references unknown endpoint_name '{resolved_endpoint_name}'"
            ),
        )

    return resolved_endpoint_name


def _resolve_pricing_template_reference_name(
    *,
    context: str,
    pricing_template_name: str | None,
    pricing_template_names_in_file: set[str],
) -> str | None:
    normalized_pricing_template_name = _normalize_reference_name(
        field="pricing_template_name", value=pricing_template_name
    )
    if normalized_pricing_template_name is None:
        return None

    if normalized_pricing_template_name not in pricing_template_names_in_file:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{context} references unknown pricing_template_name "
                f"'{normalized_pricing_template_name}'"
            ),
        )

    return normalized_pricing_template_name


def validate_import_payload(data: ConfigImportRequest) -> None:
    vendor_keys_in_file: set[str] = set()
    for vendor in data.vendors:
        if vendor.key in vendor_keys_in_file:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate vendor key: '{vendor.key}'",
            )
        vendor_keys_in_file.add(vendor.key)

    endpoint_names_in_file: set[str] = set()
    for endpoint in data.endpoints:
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

    pricing_template_names_in_file: set[str] = set()
    for template in data.pricing_templates:
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

    strategy_names_in_file: set[str] = set()
    for strategy in data.loadbalance_strategies:
        strategy_name = strategy.name.strip()
        if not strategy_name:
            raise HTTPException(
                status_code=400,
                detail="Loadbalance strategy name must not be empty",
            )
        if strategy_name in strategy_names_in_file:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate loadbalance strategy name: '{strategy_name}'",
            )
        strategy_names_in_file.add(strategy_name)

    seen_model_ids: set[str] = set()
    native_models: dict[str, str] = {}
    connection_pairs: set[tuple[str, str]] = set()

    for model in data.models:
        if model.model_id in seen_model_ids:
            raise HTTPException(
                status_code=400, detail=f"Duplicate model_id: '{model.model_id}'"
            )
        seen_model_ids.add(model.model_id)

        if model.api_family not in VALID_API_FAMILIES:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown api family: '{model.api_family}'",
            )

        if model.vendor_key not in vendor_keys_in_file:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown vendor key: '{model.vendor_key}'",
            )

        if model.model_type not in {"native", "proxy"}:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported model_type '{model.model_type}' for model '{model.model_id}'",
            )
        if model.model_type == "native":
            if model.proxy_targets:
                raise HTTPException(
                    status_code=400,
                    detail=f"Native model '{model.model_id}' must not have proxy_targets",
                )
            strategy_name = _normalize_reference_name(
                field="loadbalance_strategy_name",
                value=model.loadbalance_strategy_name,
            )
            if strategy_name is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Native model '{model.model_id}' must include "
                        "loadbalance_strategy_name"
                    ),
                )
            if strategy_name not in strategy_names_in_file:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Native model '{model.model_id}' references unknown "
                        f"loadbalance strategy '{strategy_name}'"
                    ),
                )
            native_models[model.model_id] = model.api_family
        else:
            if model.loadbalance_strategy_name is not None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Proxy model '{model.model_id}' must not include "
                        "loadbalance_strategy_name"
                    ),
                )
            if model.connections:
                raise HTTPException(
                    status_code=400,
                    detail=f"Proxy model '{model.model_id}' must not have connections",
                )
            expected_positions = list(range(len(model.proxy_targets)))
            actual_positions = [target.position for target in model.proxy_targets]
            if actual_positions != expected_positions:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Proxy model '{model.model_id}' must use contiguous proxy_targets positions starting at 0"
                    ),
                )
            seen_proxy_targets: set[str] = set()
            for target in model.proxy_targets:
                if target.target_model_id in seen_proxy_targets:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Proxy model '{model.model_id}' has duplicate proxy target '{target.target_model_id}'"
                        ),
                    )
                seen_proxy_targets.add(target.target_model_id)

        for connection in model.connections:
            _validate_optional_connection_limiter_fields(
                model_id=model.model_id,
                connection=connection,
            )
            resolved_endpoint_name = _resolve_endpoint_reference_name(
                context=f"Connection for model '{model.model_id}'",
                endpoint_name=connection.endpoint_name,
                endpoint_names_in_file=endpoint_names_in_file,
            )

            connection_pairs.add((model.model_id, resolved_endpoint_name))

            _resolve_pricing_template_reference_name(
                context=f"Connection for model '{model.model_id}'",
                pricing_template_name=connection.pricing_template_name,
                pricing_template_names_in_file=pricing_template_names_in_file,
            )

    for model in data.models:
        if model.model_type != "proxy":
            continue
        for target in model.proxy_targets:
            if target.target_model_id not in native_models:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Model '{model.model_id}' references unknown proxy target "
                        f"'{target.target_model_id}'"
                    ),
                )
            if native_models[target.target_model_id] != model.api_family:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Model '{model.model_id}' cannot target cross-api-family model "
                        f"'{target.target_model_id}'"
                    ),
                )

    if data.user_settings is not None:
        seen_fx: set[tuple[str, str]] = set()
        for mapping in data.user_settings.endpoint_fx_mappings:
            resolved_endpoint_name = _resolve_endpoint_reference_name(
                context="FX mapping",
                endpoint_name=mapping.endpoint_name,
                endpoint_names_in_file=endpoint_names_in_file,
            )
            key = (mapping.model_id, resolved_endpoint_name)
            if key in seen_fx:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Duplicate FX mapping in import for "
                        f"model_id='{mapping.model_id}', "
                        f"endpoint_name='{resolved_endpoint_name}'"
                    ),
                )
            seen_fx.add(key)
            if key not in connection_pairs:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "FX mapping must reference an imported model/endpoint connection pair: "
                        f"model_id='{mapping.model_id}', "
                        f"endpoint_name='{resolved_endpoint_name}'"
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


__all__ = ["VALID_API_FAMILIES", "validate_import_payload"]
