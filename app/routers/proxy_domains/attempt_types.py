from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from app.models.models import Connection

from .request_setup import ProxyRequestSetup


@dataclass(slots=True)
class ProxyRuntimeDependencies:
    build_upstream_headers_fn: Callable[..., dict[str, str]]
    build_upstream_url_fn: Callable[..., str]
    filter_response_headers_fn: Callable[..., dict[str, str]]
    log_request_fn: Callable[..., Awaitable[int | None]]
    mark_connection_failed_fn: Callable[..., None]
    mark_connection_recovered_fn: Callable[..., None]
    proxy_request_fn: Callable[..., Awaitable[httpx.Response]]
    record_audit_log_fn: Callable[..., Awaitable[None]]
    should_failover_fn: Callable[[int], bool]


@dataclass(slots=True)
class ProxyRequestState:
    profile_id: int
    request_path: str
    setup: ProxyRequestSetup


@dataclass(slots=True)
class ProxyAttemptTarget:
    connection: Connection
    description: str
    endpoint_body: bytes | None
    headers: dict[str, str]
    upstream_url: str


__all__ = [
    "ProxyAttemptTarget",
    "ProxyRequestState",
    "ProxyRuntimeDependencies",
]
