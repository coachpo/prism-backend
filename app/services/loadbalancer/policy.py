from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.core.config import get_settings


@dataclass(slots=True, frozen=True)
class EffectiveLoadbalancePolicy:
    strategy_type: Literal["single", "failover"]
    failover_recovery_enabled: bool
    failover_cooldown_seconds: float
    failover_failure_threshold: int
    failover_backoff_multiplier: float
    failover_max_cooldown_seconds: int
    failover_jitter_ratio: float
    failover_auth_error_cooldown_seconds: int


def _resolve_strategy_type(value: object) -> Literal["single", "failover"]:
    return "failover" if value == "failover" else "single"


def _resolve_bool(value: object, *, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _resolve_int(value: object, *, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _resolve_float(value: object, *, default: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def resolve_effective_loadbalance_policy(
    strategy: object,
) -> EffectiveLoadbalancePolicy:
    settings = get_settings()
    strategy_type = _resolve_strategy_type(getattr(strategy, "strategy_type", "single"))
    failover_recovery_enabled = strategy_type == "failover" and _resolve_bool(
        getattr(strategy, "failover_recovery_enabled", False),
        default=False,
    )

    return EffectiveLoadbalancePolicy(
        strategy_type=strategy_type,
        failover_recovery_enabled=failover_recovery_enabled,
        failover_cooldown_seconds=_resolve_float(
            getattr(strategy, "failover_cooldown_seconds", None),
            default=float(settings.failover_cooldown_seconds),
        ),
        failover_failure_threshold=_resolve_int(
            getattr(strategy, "failover_failure_threshold", None),
            default=settings.failover_failure_threshold,
        ),
        failover_backoff_multiplier=_resolve_float(
            getattr(strategy, "failover_backoff_multiplier", None),
            default=settings.failover_backoff_multiplier,
        ),
        failover_max_cooldown_seconds=_resolve_int(
            getattr(strategy, "failover_max_cooldown_seconds", None),
            default=settings.failover_max_cooldown_seconds,
        ),
        failover_jitter_ratio=_resolve_float(
            getattr(strategy, "failover_jitter_ratio", None),
            default=settings.failover_jitter_ratio,
        ),
        failover_auth_error_cooldown_seconds=_resolve_int(
            getattr(strategy, "failover_auth_error_cooldown_seconds", None),
            default=settings.failover_auth_error_cooldown_seconds,
        ),
    )


__all__ = [
    "EffectiveLoadbalancePolicy",
    "resolve_effective_loadbalance_policy",
]
