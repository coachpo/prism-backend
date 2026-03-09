import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_effective_profile_id
from app.models.models import (
    Connection,
    EndpointFxRateSetting,
    ModelConfig,
    UserSetting,
)
from app.schemas.schemas import (
    AuthSettingsResponse,
    AuthSettingsUpdate,
    CostingSettingsResponse,
    CostingSettingsUpdate,
    EmailVerificationConfirmRequest,
    EmailVerificationRequest,
    EmailVerificationResponse,
    EndpointFxMapping,
    ProxyApiKeyCreate,
    ProxyApiKeyCreateResponse,
    ProxyApiKeyResponse,
    ProxyApiKeyRotateResponse,
)
from app.services.auth_service import (
    begin_email_verification,
    build_auth_settings_response,
    confirm_email_verification,
    create_proxy_api_key,
    delete_proxy_api_key,
    get_or_create_app_auth_settings,
    list_proxy_api_keys,
    rotate_proxy_api_key,
    serialize_proxy_api_key,
    send_email_verification_otp,
    update_auth_settings,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])


async def _get_or_create_user_settings(
    db: AsyncSession,
    *,
    profile_id: int,
) -> UserSetting:
    settings_row = (
        await db.execute(
            select(UserSetting)
            .where(UserSetting.profile_id == profile_id)
            .order_by(UserSetting.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if settings_row is None:
        settings_row = UserSetting(
            profile_id=profile_id,
            report_currency_code="USD",
            report_currency_symbol="$",
            timezone_preference=None,
        )
        db.add(settings_row)
        await db.flush()
    return settings_row


@router.get("/costing", response_model=CostingSettingsResponse)
async def get_costing_settings(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    settings_row = await _get_or_create_user_settings(db, profile_id=profile_id)

    fx_rows = (
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

    return CostingSettingsResponse(
        profile_id=profile_id,
        report_currency_code=settings_row.report_currency_code,
        report_currency_symbol=settings_row.report_currency_symbol,
        timezone_preference=settings_row.timezone_preference,
        endpoint_fx_mappings=[
            EndpointFxMapping(
                model_id=row.model_id,
                endpoint_id=row.endpoint_id,
                fx_rate=row.fx_rate,
            )
            for row in fx_rows
        ],
    )


@router.put("/costing", response_model=CostingSettingsResponse)
async def update_costing_settings(
    body: CostingSettingsUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    settings_row = await _get_or_create_user_settings(db, profile_id=profile_id)

    endpoint_ids = sorted({item.endpoint_id for item in body.endpoint_fx_mappings})
    valid_pairs: set[tuple[str, int]] = set()
    if endpoint_ids:
        rows = (
            await db.execute(
                select(ModelConfig.model_id, Connection.endpoint_id)
                .join(Connection, Connection.model_config_id == ModelConfig.id)
                .where(
                    Connection.profile_id == profile_id,
                    ModelConfig.profile_id == profile_id,
                    Connection.endpoint_id.in_(endpoint_ids),
                )
            )
        ).all()
        valid_pairs = {(row.model_id, row.endpoint_id) for row in rows}

    for mapping in body.endpoint_fx_mappings:
        if (mapping.model_id, mapping.endpoint_id) not in valid_pairs:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No connection found for "
                    f"model_id='{mapping.model_id}' and endpoint_id={mapping.endpoint_id}"
                ),
            )

    settings_row.report_currency_code = body.report_currency_code
    settings_row.report_currency_symbol = body.report_currency_symbol
    settings_row.timezone_preference = body.timezone_preference

    await db.execute(
        delete(EndpointFxRateSetting).where(
            EndpointFxRateSetting.profile_id == profile_id,
        )
    )
    for mapping in body.endpoint_fx_mappings:
        db.add(
            EndpointFxRateSetting(
                profile_id=profile_id,
                model_id=mapping.model_id,
                endpoint_id=mapping.endpoint_id,
                fx_rate=mapping.fx_rate,
            )
        )

    await db.flush()

    return CostingSettingsResponse(
        profile_id=profile_id,
        report_currency_code=settings_row.report_currency_code,
        report_currency_symbol=settings_row.report_currency_symbol,
        timezone_preference=settings_row.timezone_preference,
        endpoint_fx_mappings=body.endpoint_fx_mappings,
    )


@router.get("/auth", response_model=AuthSettingsResponse)
async def get_auth_settings(
    db: Annotated[AsyncSession, Depends(get_db)],
):
    settings_row = await get_or_create_app_auth_settings(db)
    return build_auth_settings_response(settings_row)


@router.put("/auth", response_model=AuthSettingsResponse)
async def put_auth_settings(
    body: AuthSettingsUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    settings_row = await get_or_create_app_auth_settings(db)
    updated = await update_auth_settings(
        db,
        settings_row=settings_row,
        auth_enabled=body.auth_enabled,
        username=body.username,
        password=body.password,
    )
    return build_auth_settings_response(updated)


@router.post(
    "/auth/email-verification/request", response_model=EmailVerificationResponse
)
async def post_email_verification_request(
    body: EmailVerificationRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    settings_row = await get_or_create_app_auth_settings(db)
    updated, otp_code = await begin_email_verification(
        db, settings_row=settings_row, email=body.email
    )
    await asyncio.to_thread(
        send_email_verification_otp,
        recipient=body.email,
        otp_code=otp_code,
    )
    return EmailVerificationResponse(
        success=True,
        pending_email=updated.pending_email,
        email=updated.email,
        email_bound_at=updated.email_bound_at,
    )


@router.post(
    "/auth/email-verification/confirm", response_model=EmailVerificationResponse
)
async def post_email_verification_confirm(
    body: EmailVerificationConfirmRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    settings_row = await get_or_create_app_auth_settings(db)
    updated = await confirm_email_verification(
        db, settings_row=settings_row, otp_code=body.otp_code.strip()
    )
    return EmailVerificationResponse(
        success=True,
        pending_email=updated.pending_email,
        email=updated.email,
        email_bound_at=updated.email_bound_at,
    )


@router.get("/auth/proxy-keys", response_model=list[ProxyApiKeyResponse])
async def get_proxy_api_keys(
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return [serialize_proxy_api_key(row) for row in await list_proxy_api_keys(db)]


@router.post(
    "/auth/proxy-keys", response_model=ProxyApiKeyCreateResponse, status_code=201
)
async def post_proxy_api_key(
    body: ProxyApiKeyCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    auth_subject = getattr(request.state, "auth_subject", None)
    auth_subject_id: int | None = None
    if isinstance(auth_subject, dict):
        auth_subject_value = auth_subject.get("id")
        if auth_subject_value is not None:
            auth_subject_id = int(str(auth_subject_value))
    raw_key, row = await create_proxy_api_key(
        db,
        name=body.name,
        notes=body.notes,
        auth_subject_id=auth_subject_id,
    )
    return ProxyApiKeyCreateResponse(key=raw_key, item=serialize_proxy_api_key(row))


@router.post(
    "/auth/proxy-keys/{key_id}/rotate", response_model=ProxyApiKeyRotateResponse
)
async def post_rotate_proxy_api_key(
    key_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    raw_key, row = await rotate_proxy_api_key(db, key_id=key_id)
    return ProxyApiKeyRotateResponse(key=raw_key, item=serialize_proxy_api_key(row))


@router.delete("/auth/proxy-keys/{key_id}")
async def remove_proxy_api_key(
    key_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await delete_proxy_api_key(db, key_id=key_id)
    return {"deleted": True}
