from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Literal
from typing import TypeAlias
from typing import cast

from app.core.config import get_settings
from app.services.proxy_support.constants import DEFAULT_FAILOVER_STATUS_CODES

BanMode = Literal["off", "temporary", "manual"]
StrategyType: TypeAlias = Literal["single", "fill-first", "round-robin", "failover"]


@dataclass(slots=True, frozen=True)
class EffectiveLoadbalancePolicy:
    strategy_type: StrategyType
    failover_recovery_enabled: bool
    failover_status_codes: tuple[int, ...]
    failover_cooldown_seconds: float
    failover_failure_threshold: int
    failover_backoff_multiplier: float
    failover_max_cooldown_seconds: int
    failover_jitter_ratio: float
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


def _get_object_member(value: object, key: str, default: object = None) -> object:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def normalize_failover_status_codes(value: object) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError("failover_status_codes must be a list of HTTP status codes")

    items = list(value)
    normalized: set[int] = set()
    for item in items:
        if not isinstance(item, int) or isinstance(item, bool):
            raise ValueError("failover_status_codes must contain integers only")
        if item < 100 or item > 599:
            raise ValueError(
                "failover_status_codes must contain valid HTTP status codes"
            )
        normalized.add(item)

    if len(normalized) != len(items):
        raise ValueError("failover_status_codes must not contain duplicates")

    if not normalized:
        raise ValueError("failover_status_codes must contain at least one status code")

    return tuple(sorted(normalized))


def _resolve_status_codes(value: object) -> tuple[int, ...]:
    if value is None:
        return tuple(DEFAULT_FAILOVER_STATUS_CODES)
    return normalize_failover_status_codes(value)


def _build_ban_document(
    *,
    ban_mode: BanMode,
    max_cooldown_strikes_before_ban: int,
    ban_duration_seconds: int,
) -> dict[str, object]:
    if ban_mode == "manual":
        return {
            "mode": "manual",
            "max_cooldown_strikes_before_ban": max_cooldown_strikes_before_ban,
        }
    if ban_mode == "temporary":
        return {
            "mode": "temporary",
            "max_cooldown_strikes_before_ban": max_cooldown_strikes_before_ban,
            "ban_duration_seconds": ban_duration_seconds,
        }
    return {"mode": "off"}


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


def canonicalize_auto_recovery_document(
    *,
    strategy_type: StrategyType,
    auto_recovery: object,
) -> dict[str, object]:
    settings = get_settings()
    mode = _get_object_member(auto_recovery, "mode", "disabled")
    if strategy_type == "single" or mode != "enabled":
        return {"mode": "disabled"}

    cooldown = _get_object_member(auto_recovery, "cooldown", {})
    ban = _get_object_member(auto_recovery, "ban", {})
    normalized_ban_mode, normalized_ban_strikes, normalized_ban_duration = (
        normalize_strategy_ban_policy(
            strategy_type=strategy_type,
            failover_recovery_enabled=True,
            failover_ban_mode=_resolve_ban_mode(
                _get_object_member(ban, "mode", None),
                default="off",
            ),
            failover_max_cooldown_strikes_before_ban=_resolve_int(
                _get_object_member(ban, "max_cooldown_strikes_before_ban", None),
                default=0,
            ),
            failover_ban_duration_seconds=_resolve_int(
                _get_object_member(ban, "ban_duration_seconds", None),
                default=0,
            ),
        )
    )

    return {
        "mode": "enabled",
        "status_codes": list(
            _resolve_status_codes(
                _get_object_member(auto_recovery, "status_codes", None)
            )
        ),
        "cooldown": {
            "base_seconds": _resolve_int(
                _get_object_member(cooldown, "base_seconds", None),
                default=settings.failover_cooldown_seconds,
            ),
            "failure_threshold": _resolve_int(
                _get_object_member(cooldown, "failure_threshold", None),
                default=settings.failover_failure_threshold,
            ),
            "backoff_multiplier": _resolve_float(
                _get_object_member(cooldown, "backoff_multiplier", None),
                default=settings.failover_backoff_multiplier,
            ),
            "max_cooldown_seconds": _resolve_int(
                _get_object_member(cooldown, "max_cooldown_seconds", None),
                default=settings.failover_max_cooldown_seconds,
            ),
            "jitter_ratio": _resolve_float(
                _get_object_member(cooldown, "jitter_ratio", None),
                default=settings.failover_jitter_ratio,
            ),
        },
        "ban": _build_ban_document(
            ban_mode=normalized_ban_mode,
            max_cooldown_strikes_before_ban=normalized_ban_strikes,
            ban_duration_seconds=normalized_ban_duration,
        ),
    }


def serialize_auto_recovery(policy: EffectiveLoadbalancePolicy) -> dict[str, object]:
    if policy.strategy_type == "single" or not policy.failover_recovery_enabled:
        return {"mode": "disabled"}

    return {
        "mode": "enabled",
        "status_codes": list(policy.failover_status_codes),
        "cooldown": {
            "base_seconds": int(policy.failover_cooldown_seconds),
            "failure_threshold": policy.failover_failure_threshold,
            "backoff_multiplier": policy.failover_backoff_multiplier,
            "max_cooldown_seconds": policy.failover_max_cooldown_seconds,
            "jitter_ratio": policy.failover_jitter_ratio,
        },
        "ban": _build_ban_document(
            ban_mode=policy.failover_ban_mode,
            max_cooldown_strikes_before_ban=(
                policy.failover_max_cooldown_strikes_before_ban
            ),
            ban_duration_seconds=policy.failover_ban_duration_seconds,
        ),
    }


def resolve_effective_loadbalance_policy(
    strategy: object,
) -> EffectiveLoadbalancePolicy:
    settings = get_settings()
    strategy_type = _resolve_strategy_type(getattr(strategy, "strategy_type", "single"))
    document = canonicalize_auto_recovery_document(
        strategy_type=strategy_type,
        auto_recovery=getattr(strategy, "auto_recovery", {"mode": "disabled"}),
    )
    if document["mode"] != "enabled":
        return EffectiveLoadbalancePolicy(
            strategy_type=strategy_type,
            failover_recovery_enabled=False,
            failover_status_codes=tuple(DEFAULT_FAILOVER_STATUS_CODES),
            failover_cooldown_seconds=float(settings.failover_cooldown_seconds),
            failover_failure_threshold=settings.failover_failure_threshold,
            failover_backoff_multiplier=settings.failover_backoff_multiplier,
            failover_max_cooldown_seconds=settings.failover_max_cooldown_seconds,
            failover_jitter_ratio=settings.failover_jitter_ratio,
            failover_ban_mode="off",
            failover_max_cooldown_strikes_before_ban=0,
            failover_ban_duration_seconds=0,
        )

    cooldown = document["cooldown"]
    ban = document["ban"]
    return EffectiveLoadbalancePolicy(
        strategy_type=strategy_type,
        failover_recovery_enabled=True,
        failover_status_codes=tuple(cast(list[int], document["status_codes"])),
        failover_cooldown_seconds=float(cast(dict[str, Any], cooldown)["base_seconds"]),
        failover_failure_threshold=cast(
            int, cast(dict[str, Any], cooldown)["failure_threshold"]
        ),
        failover_backoff_multiplier=float(
            cast(dict[str, Any], cooldown)["backoff_multiplier"]
        ),
        failover_max_cooldown_seconds=cast(
            int, cast(dict[str, Any], cooldown)["max_cooldown_seconds"]
        ),
        failover_jitter_ratio=float(cast(dict[str, Any], cooldown)["jitter_ratio"]),
        failover_ban_mode=cast(BanMode, cast(dict[str, Any], ban)["mode"]),
        failover_max_cooldown_strikes_before_ban=cast(
            int,
            cast(dict[str, Any], ban).get("max_cooldown_strikes_before_ban", 0),
        ),
        failover_ban_duration_seconds=cast(
            int,
            cast(dict[str, Any], ban).get("ban_duration_seconds", 0),
        ),
    )


__all__ = [
    "BanMode",
    "canonicalize_auto_recovery_document",
    "EffectiveLoadbalancePolicy",
    "normalize_failover_status_codes",
    "normalize_strategy_ban_policy",
    "resolve_effective_loadbalance_policy",
    "serialize_auto_recovery",
    "StrategyType",
    "validate_strategy_ban_policy",
]
