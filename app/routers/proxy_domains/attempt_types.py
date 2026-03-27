from collections.abc import Sequence
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from app.models.models import Connection
from app.services.loadbalancer.limiter import LeaseKind, LimiterAcquireResult

from .request_setup import ProxyRequestSetup


@dataclass(slots=True)
class ProxyRuntimeDependencies:
    build_upstream_headers_fn: Callable[..., dict[str, str]]
    build_upstream_url_fn: Callable[..., str]
    acquire_connection_limit_fn: Callable[..., Awaitable[LimiterAcquireResult]]
    claim_probe_eligible_fn: Callable[..., Awaitable[None]]
    clear_connection_state_fn: Callable[..., Awaitable[bool]]
    filter_response_headers_fn: Callable[..., dict[str, str]]
    log_request_fn: Callable[..., Awaitable[int | None]]
    log_usage_request_event_fn: Callable[..., Awaitable[int | None]]
    record_connection_failure_fn: Callable[..., Awaitable[None]]
    record_connection_recovery_fn: Callable[..., Awaitable[None]]
    proxy_request_fn: Callable[..., Awaitable[httpx.Response]]
    record_audit_log_fn: Callable[..., Awaitable[None]]
    release_connection_lease_fn: Callable[..., Awaitable[bool]]
    should_failover_fn: Callable[[int, Sequence[int]], bool]


@dataclass(slots=True)
class ProxyRequestState:
    profile_id: int
    request_path: str
    setup: ProxyRequestSetup


@dataclass(slots=True)
class ProxyAttemptTarget:
    attempt_number: int
    connection: Connection
    description: str
    endpoint_body: bytes | None
    headers: dict[str, str]
    limiter_lease_token: str | None
    upstream_url: str


__all__ = [
    "ProxyAttemptTarget",
    "ProxyRequestState",
    "ProxyRuntimeDependencies",
]
