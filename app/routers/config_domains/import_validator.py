from fastapi import HTTPException

from app.schemas.schemas import ConfigImportRequest
from app.services.proxy_service import normalize_base_url, validate_base_url

VALID_PROVIDER_TYPES = {"openai", "anthropic", "gemini"}


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
    endpoint_id: int | None,
    endpoint_name: str | None,
    endpoint_id_to_name: dict[int, str],
    endpoint_names_in_file: set[str],
) -> str:
    normalized_endpoint_name = _normalize_reference_name(
        field="endpoint_name", value=endpoint_name
    )
    if endpoint_id is None and normalized_endpoint_name is None:
        raise HTTPException(
            status_code=400,
            detail=f"{context} must include endpoint_name or endpoint_id",
        )

    resolved_endpoint_name = normalized_endpoint_name
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
    pricing_template_id: int | None,
    pricing_template_name: str | None,
    pricing_template_id_to_name: dict[int, str],
    pricing_template_names_in_file: set[str],
) -> str | None:
    normalized_pricing_template_name = _normalize_reference_name(
        field="pricing_template_name", value=pricing_template_name
    )
    if pricing_template_id is None and normalized_pricing_template_name is None:
        return None

    resolved_pricing_template_name = normalized_pricing_template_name
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

    if resolved_pricing_template_name not in pricing_template_names_in_file:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{context} references unknown pricing_template_name "
                f"'{resolved_pricing_template_name}'"
            ),
        )

    return resolved_pricing_template_name


def validate_import_payload(data: ConfigImportRequest) -> None:
    endpoint_ids_in_file: set[int] = set()
    endpoint_id_to_name: dict[int, str] = {}
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

        endpoint_import_id = endpoint.endpoint_id
        if endpoint_import_id is not None:
            if endpoint_import_id in endpoint_ids_in_file:
                raise HTTPException(
                    status_code=400,
                    detail=f"Duplicate endpoint_id in import: {endpoint_import_id}",
                )
            endpoint_ids_in_file.add(endpoint_import_id)
            endpoint_id_to_name[endpoint_import_id] = endpoint_name

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
    pricing_template_id_to_name: dict[int, str] = {}
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

        template_import_id = template.pricing_template_id
        if template_import_id is not None:
            if template_import_id in pricing_template_ids_in_file:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Duplicate pricing_template_id in import: {template_import_id}"
                    ),
                )
            pricing_template_ids_in_file.add(template_import_id)
            pricing_template_id_to_name[template_import_id] = template_name

    seen_model_ids: set[str] = set()
    native_models: dict[str, str] = {}
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
            resolved_endpoint_name = _resolve_endpoint_reference_name(
                context=f"Connection for model '{model.model_id}'",
                endpoint_id=connection.endpoint_id,
                endpoint_name=connection.endpoint_name,
                endpoint_id_to_name=endpoint_id_to_name,
                endpoint_names_in_file=endpoint_names_in_file,
            )

            connection_pairs.add((model.model_id, resolved_endpoint_name))

            _resolve_pricing_template_reference_name(
                context=f"Connection for model '{model.model_id}'",
                pricing_template_id=connection.pricing_template_id,
                pricing_template_name=connection.pricing_template_name,
                pricing_template_id_to_name=pricing_template_id_to_name,
                pricing_template_names_in_file=pricing_template_names_in_file,
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
        seen_fx: set[tuple[str, str]] = set()
        for mapping in data.user_settings.endpoint_fx_mappings:
            resolved_endpoint_name = _resolve_endpoint_reference_name(
                context="FX mapping",
                endpoint_id=mapping.endpoint_id,
                endpoint_name=mapping.endpoint_name,
                endpoint_id_to_name=endpoint_id_to_name,
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


__all__ = ["VALID_PROVIDER_TYPES", "validate_import_payload"]
