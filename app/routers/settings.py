# ruff: noqa: F401
from fastapi import APIRouter

from app.routers.settings_domains import (
    auth_settings_router as _auth_settings_router,
    costing_router as _costing_router,
    email_verification_router as _email_verification_router,
    get_auth_settings,
    get_costing_settings,
    get_timezone_preference,
    get_proxy_api_keys,
    post_email_verification_confirm,
    post_email_verification_request,
    post_proxy_api_key,
    post_rotate_proxy_api_key,
    proxy_key_router as _proxy_key_router,
    put_auth_settings,
    remove_proxy_api_key,
    update_costing_settings,
    update_timezone_preference,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])
router.include_router(_costing_router)
router.include_router(_auth_settings_router)
router.include_router(_email_verification_router)
router.include_router(_proxy_key_router)

__all__ = [
    "get_auth_settings",
    "get_costing_settings",
    "get_timezone_preference",
    "get_proxy_api_keys",
    "post_email_verification_confirm",
    "post_email_verification_request",
    "post_proxy_api_key",
    "post_rotate_proxy_api_key",
    "put_auth_settings",
    "remove_proxy_api_key",
    "router",
    "update_costing_settings",
    "update_timezone_preference",
]
