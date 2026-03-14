from fastapi import HTTPException

from app.schemas.schemas import ConfigImportRequest
from app.services.proxy_service import normalize_base_url, validate_base_url

VALID_PROVIDER_TYPES = {"openai", "anthropic", "gemini"}


def validate_import_payload(data: ConfigImportRequest) -> None:
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


__all__ = ["VALID_PROVIDER_TYPES", "validate_import_payload"]
