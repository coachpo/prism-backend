from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias
from typing import Literal

from app.core.config import get_settings

BanMode = Literal["off", "temporary", "manual"]
StrategyType: TypeAlias = Literal["single", "fill-first", "round-robin", "failover"]


@dataclass(slots=True, frozen=True)
class EffectiveLoadbalancePolicy:
    strategy_type: StrategyType
    failover_recovery_enabled: bool
    failover_cooldown_seconds: float
    failover_failure_threshold: int
    failover_backoff_multiplier: float
    failover_max_cooldown_seconds: int
    failover_jitter_ratio: float
    failover_auth_error_cooldown_seconds: int
    failover_ban_mode: BanMode
    failover_max_cooldown_strikes_before_ban: int
    failover_ban_duration_seconds: int


def _resolve_strategy_type(
    value: object,
) -> StrategyType:
    if value == "fill-first":
        return "fill-first"
    if value == "round-robin":
        return "round-robin"
    if value == "failover":
        return "failover"
    return "single"


def _resolve_bool(value: object, *, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _resolve_int(value: object, *, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _resolve_float(value: object, *, default: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _resolve_ban_mode(value: object, *, default: BanMode) -> BanMode:
    if value == "off":
        return "off"
    if value == "temporary":
        return "temporary"
    if value == "manual":
        return "manual"
    return default


def validate_strategy_ban_policy(
    *,
    strategy_type: StrategyType,
    failover_recovery_enabled: bool,
    failover_ban_mode: BanMode,
    failover_max_cooldown_strikes_before_ban: int,
    failover_ban_duration_seconds: int,
) -> None:
    if strategy_type == "single" or not failover_recovery_enabled:
        return

    if failover_ban_mode == "off":
        if failover_max_cooldown_strikes_before_ban != 0:
            raise ValueError(
                "failover_ban_mode='off' requires failover_max_cooldown_strikes_before_ban=0"
            )
        if failover_ban_duration_seconds != 0:
            raise ValueError(
                "failover_ban_mode='off' requires failover_ban_duration_seconds=0"
            )
        return

    if failover_max_cooldown_strikes_before_ban < 1:
        raise ValueError(
            "failover_ban_mode requires failover_max_cooldown_strikes_before_ban >= 1"
        )

    if failover_ban_mode == "temporary":
        if failover_ban_duration_seconds < 1:
            raise ValueError(
                "failover_ban_mode='temporary' requires failover_ban_duration_seconds >= 1"
            )
        return

    if failover_ban_duration_seconds != 0:
        raise ValueError(
            "failover_ban_mode='manual' requires failover_ban_duration_seconds=0"
        )


def normalize_strategy_ban_policy(
    *,
    strategy_type: StrategyType,
    failover_recovery_enabled: bool,
    failover_ban_mode: BanMode,
    failover_max_cooldown_strikes_before_ban: int,
    failover_ban_duration_seconds: int,
) -> tuple[BanMode, int, int]:
    if strategy_type == "single" or not failover_recovery_enabled:
        return ("off", 0, 0)
    if failover_ban_mode == "off":
        return ("off", 0, 0)
    if failover_ban_mode == "manual":
        return ("manual", failover_max_cooldown_strikes_before_ban, 0)
    return (
        "temporary",
        failover_max_cooldown_strikes_before_ban,
        failover_ban_duration_seconds,
    )


def resolve_effective_loadbalance_policy(
    strategy: object,
) -> EffectiveLoadbalancePolicy:
    settings = get_settings()
    strategy_type = _resolve_strategy_type(getattr(strategy, "strategy_type", "single"))
    failover_recovery_enabled = strategy_type != "single" and _resolve_bool(
        getattr(strategy, "failover_recovery_enabled", False),
        default=False,
    )
    normalized_ban_policy = normalize_strategy_ban_policy(
        strategy_type=strategy_type,
        failover_recovery_enabled=failover_recovery_enabled,
        failover_ban_mode=_resolve_ban_mode(
            getattr(strategy, "failover_ban_mode", None),
            default="off",
        ),
        failover_max_cooldown_strikes_before_ban=_resolve_int(
            getattr(strategy, "failover_max_cooldown_strikes_before_ban", None),
            default=0,
        ),
        failover_ban_duration_seconds=_resolve_int(
            getattr(strategy, "failover_ban_duration_seconds", None),
            default=0,
        ),
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
        failover_ban_mode=normalized_ban_policy[0],
        failover_max_cooldown_strikes_before_ban=normalized_ban_policy[1],
        failover_ban_duration_seconds=normalized_ban_policy[2],
    )


__all__ = [
    "BanMode",
    "EffectiveLoadbalancePolicy",
    "normalize_strategy_ban_policy",
    "resolve_effective_loadbalance_policy",
    "StrategyType",
    "validate_strategy_ban_policy",
]
