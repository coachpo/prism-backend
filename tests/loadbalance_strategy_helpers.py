from __future__ import annotations

from itertools import count
from typing import Literal

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


def make_loadbalance_strategy(
    *,
    profile_id: int | None = None,
    profile: object | None = None,
    strategy_type: Literal[
        "single", "fill-first", "round-robin", "failover"
    ] = "single",
    auto_recovery: dict[str, object] | None = None,
    failover_recovery_enabled: bool | None = None,
    failover_status_codes: list[int] | None = None,
    name: str | None = None,
) -> LoadbalanceStrategy:
    if failover_recovery_enabled is None:
        failover_recovery_enabled = strategy_type != "single"

    resolved_auto_recovery = auto_recovery or (
        make_auto_recovery_enabled(status_codes=failover_status_codes)
        if strategy_type != "single" and failover_recovery_enabled
        else make_auto_recovery_disabled()
    )

    payload: dict[str, object] = {
        "name": name or f"{strategy_type}-strategy-{next(_strategy_counter)}",
        "strategy_type": strategy_type,
        "auto_recovery": resolved_auto_recovery,
    }
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
    "make_loadbalance_strategy",
]
