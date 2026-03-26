from collections.abc import Mapping
import json
import logging
import re

from app.core.crypto import decrypt_secret
from app.models.models import Connection, Endpoint, HeaderBlocklistRule

from .constants import API_FAMILY_AUTH, CLIENT_AUTH_HEADERS, HOP_BY_HOP_HEADERS

logger = logging.getLogger("app.services.proxy_service")

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1F\x7F]")


def _normalize_header_value(value: object | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if _CONTROL_CHAR_RE.search(normalized):
        return None
    return normalized


def _normalize_header_values(headers: Mapping[str, object]) -> dict[str, str]:
    normalized_headers: dict[str, str] = {}
    for key, raw_value in headers.items():
        normalized_value = _normalize_header_value(raw_value)
        if normalized_value is None:
            logger.warning("Dropping header '%s' due to invalid value", key)
            continue
        normalized_headers[key] = normalized_value
    return normalized_headers


def header_is_blocked(name: str, rules: list[HeaderBlocklistRule]) -> bool:
    name_lower = name.lower()
    for rule in rules:
        if not rule.enabled:
            continue
        if rule.match_type == "exact" and name_lower == rule.pattern:
            return True
        if rule.match_type == "prefix" and name_lower.startswith(rule.pattern):
            return True
    return False


def sanitize_headers(
    headers: dict[str, str], rules: list[HeaderBlocklistRule]
) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        if header_is_blocked(key, rules):
            logger.debug("Blocked header: %s", key)
        else:
            result[key] = value
    return result


def build_upstream_headers(
    connection: Connection | Endpoint,
    api_family: str,
    client_headers: dict[str, str] | None = None,
    blocklist_rules: list[HeaderBlocklistRule] | None = None,
    endpoint: Endpoint | None = None,
    request_compressed: bool = True,
) -> dict[str, str]:
    auth_key = getattr(connection, "auth_type", None) or api_family
    config = API_FAMILY_AUTH.get(auth_key)
    if config is None:
        raise ValueError(f"Unsupported auth_type: {auth_key}")
    endpoint_obj = endpoint or connection
    api_key = decrypt_secret(getattr(endpoint_obj, "api_key", None))
    custom_headers = getattr(connection, "custom_headers", None)

    proxy_controlled_headers = {
        config["auth_header"].lower(),
        *(k.lower() for k in config["extra_headers"]),
    }

    headers: dict[str, str] = {}

    if client_headers:
        for key, value in client_headers.items():
            key_lower = key.lower()
            if (
                key_lower not in HOP_BY_HOP_HEADERS
                and key_lower not in CLIENT_AUTH_HEADERS
                and key_lower != "content-length"
                and key_lower != "accept-encoding"
                and key_lower not in proxy_controlled_headers
            ):
                headers[key] = value

    if blocklist_rules:
        headers = sanitize_headers(headers, blocklist_rules)

    normalized_api_key = _normalize_header_value(api_key) or ""
    headers[config["auth_header"]] = f"{config['auth_prefix']}{normalized_api_key}"
    headers.update(config["extra_headers"])

    if custom_headers:
        try:
            custom = json.loads(custom_headers)
            if isinstance(custom, dict):
                for key, raw_value in custom.items():
                    normalized_custom_value = _normalize_header_value(raw_value)
                    if normalized_custom_value is None:
                        logger.warning(
                            "Skipping custom header '%s' due to invalid value", key
                        )
                        continue
                    headers[key] = normalized_custom_value
        except (json.JSONDecodeError, TypeError):
            pass

    if blocklist_rules:
        auth_header_lower = config["auth_header"].lower()
        extra_lower = {key.lower() for key in config["extra_headers"]}
        protected = {auth_header_lower} | extra_lower

        sanitized = {}
        for key, value in headers.items():
            if key.lower() in protected:
                sanitized[key] = value
            elif not header_is_blocked(key, blocklist_rules):
                sanitized[key] = value
            else:
                logger.debug("Blocked header (post-merge): %s", key)
        headers = sanitized

    if not request_compressed:
        headers["Accept-Encoding"] = "identity"

    return _normalize_header_values(headers)


__all__ = [
    "build_upstream_headers",
    "header_is_blocked",
    "sanitize_headers",
]
