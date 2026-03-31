from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.models.models import Connection, Endpoint
from app.schemas.schemas import ConnectionCreate, ConnectionUpdate
from app.services.proxy_service import normalize_base_url, validate_base_url

from ..crud_dependencies import ConnectionCrudDependencies


CONNECTION_LIMITER_FIELDS = (
    "qps_limit",
    "max_in_flight_non_stream",
    "max_in_flight_stream",
)


def build_connection_limiter_data(
    *,
    body: ConnectionCreate | ConnectionUpdate,
    exclude_unset: bool,
) -> dict[str, int | None]:
    limiter_data: dict[str, int | None] = {}
    field_names = body.model_fields_set if exclude_unset else CONNECTION_LIMITER_FIELDS
    for field_name in CONNECTION_LIMITER_FIELDS:
        if field_name not in field_names:
            continue
        limiter_value = getattr(body, field_name)
        if limiter_value is not None and limiter_value < 1:
            raise HTTPException(
                status_code=422,
                detail=f"{field_name} must be >= 1 when provided",
            )
        limiter_data[field_name] = limiter_value
    return limiter_data


async def load_profile_endpoint_or_404(
    db: AsyncSession,
    *,
    profile_id: int,
    endpoint_id: int,
) -> Endpoint:
    endpoint_result = await db.execute(
        select(Endpoint).where(
            Endpoint.id == endpoint_id,
            Endpoint.profile_id == profile_id,
        )
    )
    endpoint = endpoint_result.scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return endpoint


async def resolve_create_endpoint(
    *,
    body: ConnectionCreate,
    db: AsyncSession,
    profile_id: int,
    deps: ConnectionCrudDependencies,
) -> Endpoint:
    if body.endpoint_id is not None:
        return await load_profile_endpoint_or_404(
            db,
            profile_id=profile_id,
            endpoint_id=body.endpoint_id,
        )
    if body.endpoint_create is not None:
        return await deps.create_endpoint_from_inline_fn(
            db,
            profile_id=profile_id,
            endpoint_name=body.endpoint_create.name,
            base_url=body.endpoint_create.base_url,
            api_key=body.endpoint_create.api_key,
        )

    raise HTTPException(
        status_code=422,
        detail="Exactly one of endpoint_id or endpoint_create is required",
    )


def build_preview_endpoint_from_inline(
    *,
    profile_id: int,
    endpoint_name: str,
    base_url: str,
    api_key: str,
) -> Endpoint:
    clean_name = endpoint_name.strip()
    if not clean_name:
        raise HTTPException(
            status_code=422, detail="endpoint_create.name must not be empty"
        )

    normalized_url = normalize_base_url(base_url)
    url_warnings = validate_base_url(normalized_url)
    if url_warnings:
        raise HTTPException(status_code=422, detail="; ".join(url_warnings))

    return Endpoint(
        profile_id=profile_id,
        name=clean_name,
        base_url=normalized_url,
        api_key=encrypt_secret(api_key),
        position=0,
    )


async def resolve_preview_endpoint(
    *,
    body: ConnectionCreate,
    db: AsyncSession,
    profile_id: int,
) -> Endpoint:
    if body.endpoint_id is not None:
        return await load_profile_endpoint_or_404(
            db,
            profile_id=profile_id,
            endpoint_id=body.endpoint_id,
        )
    if body.endpoint_create is not None:
        return build_preview_endpoint_from_inline(
            profile_id=profile_id,
            endpoint_name=body.endpoint_create.name,
            base_url=body.endpoint_create.base_url,
            api_key=body.endpoint_create.api_key,
        )

    raise HTTPException(
        status_code=422,
        detail="Exactly one of endpoint_id or endpoint_create is required",
    )


async def build_connection_update_data(
    *,
    body: ConnectionUpdate,
    db: AsyncSession,
    profile_id: int,
    deps: ConnectionCrudDependencies,
) -> dict[str, object]:
    update_data = body.model_dump(exclude_unset=True)

    inline_endpoint_payload = update_data.pop("endpoint_create", None)
    if inline_endpoint_payload is not None:
        endpoint = await deps.create_endpoint_from_inline_fn(
            db,
            profile_id=profile_id,
            endpoint_name=inline_endpoint_payload["name"],
            base_url=inline_endpoint_payload["base_url"],
            api_key=inline_endpoint_payload["api_key"],
        )
        update_data["endpoint_id"] = endpoint.id

    if "endpoint_id" in update_data:
        await load_profile_endpoint_or_404(
            db,
            profile_id=profile_id,
            endpoint_id=update_data["endpoint_id"],
        )

    if "pricing_template_id" in update_data:
        update_data["pricing_template_id"] = await deps.validate_pricing_template_id_fn(
            db,
            profile_id=profile_id,
            pricing_template_id=update_data["pricing_template_id"],
        )

    if "custom_headers" in update_data:
        update_data["custom_headers"] = deps.serialize_custom_headers_fn(
            update_data["custom_headers"]
        )

    for field_name in CONNECTION_LIMITER_FIELDS:
        update_data.pop(field_name, None)
    update_data.update(build_connection_limiter_data(body=body, exclude_unset=True))

    return update_data


def should_clear_recovery_state(
    connection: Connection,
    update_data: dict[str, object],
) -> bool:
    return any(
        (
            "is_active" in update_data
            and update_data["is_active"] != connection.is_active,
            "endpoint_id" in update_data
            and update_data["endpoint_id"] != connection.endpoint_id,
            "auth_type" in update_data
            and update_data["auth_type"] != connection.auth_type,
            "custom_headers" in update_data
            and update_data["custom_headers"] != connection.custom_headers,
        )
    )


__all__ = [
    "build_preview_endpoint_from_inline",
    "build_connection_update_data",
    "build_connection_limiter_data",
    "load_profile_endpoint_or_404",
    "resolve_create_endpoint",
    "resolve_preview_endpoint",
    "should_clear_recovery_state",
]
