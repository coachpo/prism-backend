from __future__ import annotations

from itertools import count
from typing import Literal, cast

from app.models.models import LoadbalanceStrategy

_strategy_counter = count(1)
DEFAULT_FAILOVER_STATUS_CODES = [403, 422, 429, 500, 502, 503, 504, 529]


def make_auto_recovery_disabled() -> dict[str, object]:
    return {"mode": "disabled"}


def make_auto_recovery_enabled(
    *,
    status_codes: list[int] | None = None,
    base_seconds: int = 60,
    failure_threshold: int = 2,
    backoff_multiplier: float = 2.0,
    max_cooldown_seconds: int = 900,
    jitter_ratio: float = 0.2,
    ban_mode: Literal["off", "manual", "temporary"] = "off",
    max_cooldown_strikes_before_ban: int = 0,
    ban_duration_seconds: int = 0,
) -> dict[str, object]:
    ban: dict[str, object] = {"mode": ban_mode}
    if ban_mode in {"manual", "temporary"}:
        ban["max_cooldown_strikes_before_ban"] = max_cooldown_strikes_before_ban
    if ban_mode == "temporary":
        ban["ban_duration_seconds"] = ban_duration_seconds

    return {
        "mode": "enabled",
        "status_codes": list(status_codes or DEFAULT_FAILOVER_STATUS_CODES),
        "cooldown": {
            "base_seconds": base_seconds,
            "failure_threshold": failure_threshold,
            "backoff_multiplier": backoff_multiplier,
            "max_cooldown_seconds": max_cooldown_seconds,
            "jitter_ratio": jitter_ratio,
        },
        "ban": ban,
    }


def make_routing_policy_adaptive(
    *,
    routing_objective: Literal[
        "minimize_latency", "maximize_availability"
    ] = "minimize_latency",
    deadline_budget_ms: int = 30_000,
    hedge_enabled: bool = False,
    hedge_delay_ms: int = 1_500,
    max_additional_attempts: int = 1,
    failure_status_codes: list[int] | None = None,
    base_open_seconds: int = 60,
    failure_threshold: int = 2,
    backoff_multiplier: float = 2.0,
    max_open_seconds: int = 900,
    jitter_ratio: float = 0.2,
    ban_mode: Literal["off", "manual", "temporary"] = "off",
    max_open_strikes_before_ban: int = 0,
    ban_duration_seconds: int = 0,
    respect_qps_limit: bool = True,
    respect_in_flight_limits: bool = True,
    monitoring_enabled: bool = True,
    stale_after_seconds: int = 300,
    endpoint_ping_weight: float = 1.0,
    conversation_delay_weight: float = 1.0,
    failure_penalty_weight: float = 2.0,
) -> dict[str, object]:
    circuit_breaker: dict[str, object] = {
        "failure_status_codes": sorted(
            list(failure_status_codes or DEFAULT_FAILOVER_STATUS_CODES)
        ),
        "base_open_seconds": base_open_seconds,
        "failure_threshold": failure_threshold,
        "backoff_multiplier": backoff_multiplier,
        "max_open_seconds": max_open_seconds,
        "jitter_ratio": jitter_ratio,
        "ban_mode": ban_mode,
        "max_open_strikes_before_ban": max_open_strikes_before_ban,
        "ban_duration_seconds": ban_duration_seconds,
    }

    return {
        "kind": "adaptive",
        "routing_objective": routing_objective,
        "deadline_budget_ms": deadline_budget_ms,
        "hedge": {
            "enabled": hedge_enabled,
            "delay_ms": hedge_delay_ms,
            "max_additional_attempts": max_additional_attempts,
        },
        "circuit_breaker": circuit_breaker,
        "admission": {
            "respect_qps_limit": respect_qps_limit,
            "respect_in_flight_limits": respect_in_flight_limits,
        },
        "monitoring": {
            "enabled": monitoring_enabled,
            "stale_after_seconds": stale_after_seconds,
            "endpoint_ping_weight": endpoint_ping_weight,
            "conversation_delay_weight": conversation_delay_weight,
            "failure_penalty_weight": failure_penalty_weight,
        },
    }


def make_loadbalance_strategy(
    *,
    profile_id: int | None = None,
    profile: object | None = None,
    strategy_type: Literal["single", "fill-first", "round-robin", "failover"]
    | None = None,
    auto_recovery: dict[str, object] | None = None,
    failover_recovery_enabled: bool | None = None,
    failover_status_codes: list[int] | None = None,
    routing_policy: dict[str, object] | None = None,
    name: str | None = None,
) -> LoadbalanceStrategy:
    payload: dict[str, object] = {
        "name": name
        or f"{strategy_type or 'adaptive'}-strategy-{next(_strategy_counter)}",
        "strategy_type": "adaptive" if routing_policy is not None else "legacy",
        "legacy_strategy_type": None,
        "auto_recovery": None,
        "routing_policy": routing_policy,
    }
    if routing_policy is None:
        resolved_strategy_type = strategy_type or "single"
        failover_enabled = (
            failover_recovery_enabled
            if failover_recovery_enabled is not None
            else resolved_strategy_type != "single"
        )
        payload["legacy_strategy_type"] = (
            "round-robin"
            if resolved_strategy_type == "failover"
            else resolved_strategy_type
        )
        payload["auto_recovery"] = auto_recovery or (
            make_auto_recovery_enabled(status_codes=failover_status_codes)
            if failover_enabled
            else make_auto_recovery_disabled()
        )
    if profile is not None:
        payload["profile"] = profile
    elif profile_id is not None:
        payload["profile_id"] = profile_id
    else:
        raise ValueError("Either profile or profile_id is required")

    return LoadbalanceStrategy(**payload)


__all__ = [
    "DEFAULT_FAILOVER_STATUS_CODES",
    "make_auto_recovery_disabled",
    "make_auto_recovery_enabled",
    "make_routing_policy_adaptive",
    "make_loadbalance_strategy",
]
