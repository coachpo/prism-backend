from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from typing import Literal
from typing import TypeAlias
from typing import cast

from app.core.config import get_settings
from app.services.proxy_support.constants import DEFAULT_FAILOVER_STATUS_CODES

BanMode = Literal["off", "temporary", "manual"]
RoutingObjective = Literal["minimize_latency", "maximize_availability"]
StrategyType: TypeAlias = Literal[
    "single", "fill-first", "round-robin", "failover", "adaptive"
]

_DEFAULT_DEADLINE_BUDGET_MS = 30_000
_DEFAULT_HEDGE_DELAY_MS = 1_500
_DEFAULT_MAX_ADDITIONAL_ATTEMPTS = 1
_DEFAULT_MONITORING_STALE_AFTER_SECONDS = 300
_DEFAULT_MONITORING_ENDPOINT_PING_WEIGHT = 1.0
_DEFAULT_MONITORING_CONVERSATION_DELAY_WEIGHT = 1.0
_DEFAULT_MONITORING_FAILURE_PENALTY_WEIGHT = 2.0


@dataclass(slots=True, frozen=True)
class EffectiveLoadbalancePolicy:
    kind: Literal["adaptive"]
    routing_objective: RoutingObjective
    deadline_budget_ms: int
    hedge_enabled: bool
    hedge_delay_ms: int
    max_additional_attempts: int
    circuit_failure_status_codes: tuple[int, ...]
    circuit_base_open_seconds: float
    circuit_failure_threshold: int
    circuit_backoff_multiplier: float
    circuit_max_open_seconds: int
    circuit_jitter_ratio: float
    circuit_ban_mode: BanMode
    circuit_max_open_strikes_before_ban: int
    circuit_ban_duration_seconds: int
    admission_respect_qps_limit: bool
    admission_respect_in_flight_limits: bool
    monitoring_enabled: bool
    monitoring_stale_after_seconds: int
    monitoring_endpoint_ping_weight: float
    monitoring_conversation_delay_weight: float
    monitoring_failure_penalty_weight: float

    @property
    def strategy_type(self) -> str:
        return self.kind

    @property
    def failover_recovery_enabled(self) -> bool:
        return True

    @property
    def failover_status_codes(self) -> tuple[int, ...]:
        return self.circuit_failure_status_codes

    @property
    def failover_cooldown_seconds(self) -> float:
        return self.circuit_base_open_seconds

    @property
    def failover_failure_threshold(self) -> int:
        return self.circuit_failure_threshold

    @property
    def failover_backoff_multiplier(self) -> float:
        return self.circuit_backoff_multiplier

    @property
    def failover_max_cooldown_seconds(self) -> int:
        return self.circuit_max_open_seconds

    @property
    def failover_jitter_ratio(self) -> float:
        return self.circuit_jitter_ratio

    @property
    def failover_ban_mode(self) -> BanMode:
        return self.circuit_ban_mode

    @property
    def failover_max_cooldown_strikes_before_ban(self) -> int:
        return self.circuit_max_open_strikes_before_ban

    @property
    def failover_ban_duration_seconds(self) -> int:
        return self.circuit_ban_duration_seconds


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


def _resolve_routing_objective(
    value: object,
    *,
    default: RoutingObjective,
) -> RoutingObjective:
    if value == "maximize_availability":
        return "maximize_availability"
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


def _legacy_routing_policy_document(
    *,
    strategy_type: StrategyType,
    auto_recovery: object,
) -> dict[str, object]:
    settings = get_settings()
    legacy_document = canonicalize_auto_recovery_document(
        strategy_type=strategy_type,
        auto_recovery=auto_recovery,
    )
    cooldown = cast(dict[str, object], legacy_document.get("cooldown", {}))
    ban = cast(dict[str, object], legacy_document.get("ban", {}))
    status_codes = cast(
        list[int],
        legacy_document.get("status_codes", list(DEFAULT_FAILOVER_STATUS_CODES)),
    )
    if not status_codes:
        status_codes = list(DEFAULT_FAILOVER_STATUS_CODES)
    return {
        "kind": "adaptive",
        "routing_objective": "minimize_latency",
        "deadline_budget_ms": _DEFAULT_DEADLINE_BUDGET_MS,
        "hedge": {
            "enabled": False,
            "delay_ms": _DEFAULT_HEDGE_DELAY_MS,
            "max_additional_attempts": _DEFAULT_MAX_ADDITIONAL_ATTEMPTS,
        },
        "circuit_breaker": {
            "failure_status_codes": list(status_codes),
            "base_open_seconds": _resolve_int(
                cooldown.get("base_seconds"),
                default=settings.failover_cooldown_seconds,
            ),
            "failure_threshold": _resolve_int(
                cooldown.get("failure_threshold"),
                default=settings.failover_failure_threshold,
            ),
            "backoff_multiplier": _resolve_float(
                cooldown.get("backoff_multiplier"),
                default=settings.failover_backoff_multiplier,
            ),
            "max_open_seconds": _resolve_int(
                cooldown.get("max_cooldown_seconds"),
                default=settings.failover_max_cooldown_seconds,
            ),
            "jitter_ratio": _resolve_float(
                cooldown.get("jitter_ratio"),
                default=settings.failover_jitter_ratio,
            ),
            "ban_mode": _resolve_ban_mode(ban.get("mode"), default="off"),
            "max_open_strikes_before_ban": _resolve_int(
                ban.get("max_cooldown_strikes_before_ban"),
                default=0,
            ),
            "ban_duration_seconds": _resolve_int(
                ban.get("ban_duration_seconds"),
                default=0,
            ),
        },
        "admission": {
            "respect_qps_limit": True,
            "respect_in_flight_limits": True,
        },
        "monitoring": {
            "enabled": True,
            "stale_after_seconds": _DEFAULT_MONITORING_STALE_AFTER_SECONDS,
            "endpoint_ping_weight": _DEFAULT_MONITORING_ENDPOINT_PING_WEIGHT,
            "conversation_delay_weight": _DEFAULT_MONITORING_CONVERSATION_DELAY_WEIGHT,
            "failure_penalty_weight": _DEFAULT_MONITORING_FAILURE_PENALTY_WEIGHT,
        },
        "legacy_strategy_type": strategy_type,
        "legacy_auto_recovery": legacy_document,
    }


def canonicalize_routing_policy_document(
    routing_policy: object | None,
    *,
    legacy_strategy_type: StrategyType | None = None,
    legacy_auto_recovery: object | None = None,
) -> dict[str, object]:
    if routing_policy is None:
        return _legacy_routing_policy_document(
            strategy_type=legacy_strategy_type or "failover",
            auto_recovery=legacy_auto_recovery or {"mode": "enabled"},
        )

    settings = get_settings()
    hedge = _get_object_member(routing_policy, "hedge", {})
    circuit_breaker = _get_object_member(routing_policy, "circuit_breaker", {})
    admission = _get_object_member(routing_policy, "admission", {})
    monitoring = _get_object_member(routing_policy, "monitoring", {})

    normalized_ban_mode = _resolve_ban_mode(
        _get_object_member(circuit_breaker, "ban_mode", None),
        default="off",
    )
    max_open_strikes_before_ban = _resolve_int(
        _get_object_member(circuit_breaker, "max_open_strikes_before_ban", None),
        default=0,
    )
    ban_duration_seconds = _resolve_int(
        _get_object_member(circuit_breaker, "ban_duration_seconds", None),
        default=0,
    )
    validate_strategy_ban_policy(
        strategy_type="adaptive",
        failover_recovery_enabled=True,
        failover_ban_mode=normalized_ban_mode,
        failover_max_cooldown_strikes_before_ban=max_open_strikes_before_ban,
        failover_ban_duration_seconds=ban_duration_seconds,
    )

    return {
        "kind": "adaptive",
        "routing_objective": _resolve_routing_objective(
            _get_object_member(routing_policy, "routing_objective", None),
            default="minimize_latency",
        ),
        "deadline_budget_ms": _resolve_int(
            _get_object_member(routing_policy, "deadline_budget_ms", None),
            default=_DEFAULT_DEADLINE_BUDGET_MS,
        ),
        "hedge": {
            "enabled": _resolve_bool(
                _get_object_member(hedge, "enabled", None),
                default=False,
            ),
            "delay_ms": _resolve_int(
                _get_object_member(hedge, "delay_ms", None),
                default=_DEFAULT_HEDGE_DELAY_MS,
            ),
            "max_additional_attempts": _resolve_int(
                _get_object_member(hedge, "max_additional_attempts", None),
                default=_DEFAULT_MAX_ADDITIONAL_ATTEMPTS,
            ),
        },
        "circuit_breaker": {
            "failure_status_codes": list(
                _resolve_status_codes(
                    _get_object_member(circuit_breaker, "failure_status_codes", None)
                )
            ),
            "base_open_seconds": _resolve_int(
                _get_object_member(circuit_breaker, "base_open_seconds", None),
                default=settings.failover_cooldown_seconds,
            ),
            "failure_threshold": _resolve_int(
                _get_object_member(circuit_breaker, "failure_threshold", None),
                default=settings.failover_failure_threshold,
            ),
            "backoff_multiplier": _resolve_float(
                _get_object_member(circuit_breaker, "backoff_multiplier", None),
                default=settings.failover_backoff_multiplier,
            ),
            "max_open_seconds": _resolve_int(
                _get_object_member(circuit_breaker, "max_open_seconds", None),
                default=settings.failover_max_cooldown_seconds,
            ),
            "jitter_ratio": _resolve_float(
                _get_object_member(circuit_breaker, "jitter_ratio", None),
                default=settings.failover_jitter_ratio,
            ),
            "ban_mode": normalized_ban_mode,
            "max_open_strikes_before_ban": max_open_strikes_before_ban,
            "ban_duration_seconds": ban_duration_seconds,
        },
        "admission": {
            "respect_qps_limit": _resolve_bool(
                _get_object_member(admission, "respect_qps_limit", None),
                default=True,
            ),
            "respect_in_flight_limits": _resolve_bool(
                _get_object_member(admission, "respect_in_flight_limits", None),
                default=True,
            ),
        },
        "monitoring": {
            "enabled": _resolve_bool(
                _get_object_member(monitoring, "enabled", None),
                default=True,
            ),
            "stale_after_seconds": _resolve_int(
                _get_object_member(monitoring, "stale_after_seconds", None),
                default=_DEFAULT_MONITORING_STALE_AFTER_SECONDS,
            ),
            "endpoint_ping_weight": _resolve_float(
                _get_object_member(monitoring, "endpoint_ping_weight", None),
                default=_DEFAULT_MONITORING_ENDPOINT_PING_WEIGHT,
            ),
            "conversation_delay_weight": _resolve_float(
                _get_object_member(monitoring, "conversation_delay_weight", None),
                default=_DEFAULT_MONITORING_CONVERSATION_DELAY_WEIGHT,
            ),
            "failure_penalty_weight": _resolve_float(
                _get_object_member(monitoring, "failure_penalty_weight", None),
                default=_DEFAULT_MONITORING_FAILURE_PENALTY_WEIGHT,
            ),
        },
    }


def serialize_auto_recovery(policy: EffectiveLoadbalancePolicy) -> dict[str, object]:
    return {
        "mode": "enabled",
        "status_codes": list(policy.circuit_failure_status_codes),
        "cooldown": {
            "base_seconds": int(policy.circuit_base_open_seconds),
            "failure_threshold": policy.circuit_failure_threshold,
            "backoff_multiplier": policy.circuit_backoff_multiplier,
            "max_cooldown_seconds": policy.circuit_max_open_seconds,
            "jitter_ratio": policy.circuit_jitter_ratio,
        },
        "ban": _build_ban_document(
            ban_mode=policy.circuit_ban_mode,
            max_cooldown_strikes_before_ban=policy.circuit_max_open_strikes_before_ban,
            ban_duration_seconds=policy.circuit_ban_duration_seconds,
        ),
    }


def serialize_routing_policy(policy: EffectiveLoadbalancePolicy) -> dict[str, object]:
    return {
        "kind": policy.kind,
        "routing_objective": policy.routing_objective,
        "deadline_budget_ms": policy.deadline_budget_ms,
        "hedge": {
            "enabled": policy.hedge_enabled,
            "delay_ms": policy.hedge_delay_ms,
            "max_additional_attempts": policy.max_additional_attempts,
        },
        "circuit_breaker": {
            "failure_status_codes": list(policy.circuit_failure_status_codes),
            "base_open_seconds": int(policy.circuit_base_open_seconds),
            "failure_threshold": policy.circuit_failure_threshold,
            "backoff_multiplier": policy.circuit_backoff_multiplier,
            "max_open_seconds": policy.circuit_max_open_seconds,
            "jitter_ratio": policy.circuit_jitter_ratio,
            "ban_mode": policy.circuit_ban_mode,
            "max_open_strikes_before_ban": policy.circuit_max_open_strikes_before_ban,
            "ban_duration_seconds": policy.circuit_ban_duration_seconds,
        },
        "admission": {
            "respect_qps_limit": policy.admission_respect_qps_limit,
            "respect_in_flight_limits": policy.admission_respect_in_flight_limits,
        },
        "monitoring": {
            "enabled": policy.monitoring_enabled,
            "stale_after_seconds": policy.monitoring_stale_after_seconds,
            "endpoint_ping_weight": policy.monitoring_endpoint_ping_weight,
            "conversation_delay_weight": (policy.monitoring_conversation_delay_weight),
            "failure_penalty_weight": policy.monitoring_failure_penalty_weight,
        },
    }


def resolve_effective_loadbalance_policy(
    strategy: object,
) -> EffectiveLoadbalancePolicy:
    routing_policy = getattr(strategy, "routing_policy", None)
    if routing_policy is None:
        legacy_strategy_type = cast(
            StrategyType,
            getattr(strategy, "strategy_type", "failover"),
        )
        routing_policy = canonicalize_routing_policy_document(
            None,
            legacy_strategy_type=legacy_strategy_type,
            legacy_auto_recovery=getattr(
                strategy, "auto_recovery", {"mode": "enabled"}
            ),
        )
    else:
        routing_policy = canonicalize_routing_policy_document(routing_policy)

    hedge = cast(dict[str, Any], routing_policy["hedge"])
    circuit_breaker = cast(dict[str, Any], routing_policy["circuit_breaker"])
    admission = cast(dict[str, Any], routing_policy["admission"])
    monitoring = cast(dict[str, Any], routing_policy["monitoring"])

    return EffectiveLoadbalancePolicy(
        kind="adaptive",
        routing_objective=cast(RoutingObjective, routing_policy["routing_objective"]),
        deadline_budget_ms=cast(int, routing_policy["deadline_budget_ms"]),
        hedge_enabled=cast(bool, hedge["enabled"]),
        hedge_delay_ms=cast(int, hedge["delay_ms"]),
        max_additional_attempts=cast(int, hedge["max_additional_attempts"]),
        circuit_failure_status_codes=tuple(
            cast(list[int], circuit_breaker["failure_status_codes"])
        ),
        circuit_base_open_seconds=float(circuit_breaker["base_open_seconds"]),
        circuit_failure_threshold=cast(int, circuit_breaker["failure_threshold"]),
        circuit_backoff_multiplier=float(circuit_breaker["backoff_multiplier"]),
        circuit_max_open_seconds=cast(int, circuit_breaker["max_open_seconds"]),
        circuit_jitter_ratio=float(circuit_breaker["jitter_ratio"]),
        circuit_ban_mode=cast(BanMode, circuit_breaker["ban_mode"]),
        circuit_max_open_strikes_before_ban=cast(
            int,
            circuit_breaker["max_open_strikes_before_ban"],
        ),
        circuit_ban_duration_seconds=cast(
            int,
            circuit_breaker["ban_duration_seconds"],
        ),
        admission_respect_qps_limit=cast(bool, admission["respect_qps_limit"]),
        admission_respect_in_flight_limits=cast(
            bool,
            admission["respect_in_flight_limits"],
        ),
        monitoring_enabled=cast(bool, monitoring["enabled"]),
        monitoring_stale_after_seconds=cast(int, monitoring["stale_after_seconds"]),
        monitoring_endpoint_ping_weight=float(monitoring["endpoint_ping_weight"]),
        monitoring_conversation_delay_weight=float(
            monitoring["conversation_delay_weight"]
        ),
        monitoring_failure_penalty_weight=float(monitoring["failure_penalty_weight"]),
    )


def build_default_routing_policy_document() -> dict[str, object]:
    return canonicalize_routing_policy_document(
        SimpleNamespace(
            kind="adaptive",
            routing_objective="minimize_latency",
            deadline_budget_ms=_DEFAULT_DEADLINE_BUDGET_MS,
            hedge={
                "enabled": False,
                "delay_ms": _DEFAULT_HEDGE_DELAY_MS,
                "max_additional_attempts": _DEFAULT_MAX_ADDITIONAL_ATTEMPTS,
            },
            monitoring={
                "enabled": True,
                "stale_after_seconds": _DEFAULT_MONITORING_STALE_AFTER_SECONDS,
                "endpoint_ping_weight": _DEFAULT_MONITORING_ENDPOINT_PING_WEIGHT,
                "conversation_delay_weight": (
                    _DEFAULT_MONITORING_CONVERSATION_DELAY_WEIGHT
                ),
                "failure_penalty_weight": _DEFAULT_MONITORING_FAILURE_PENALTY_WEIGHT,
            },
        )
    )


__all__ = [
    "BanMode",
    "build_default_routing_policy_document",
    "canonicalize_auto_recovery_document",
    "canonicalize_routing_policy_document",
    "EffectiveLoadbalancePolicy",
    "normalize_failover_status_codes",
    "normalize_strategy_ban_policy",
    "resolve_effective_loadbalance_policy",
    "RoutingObjective",
    "serialize_auto_recovery",
    "serialize_routing_policy",
    "StrategyType",
    "validate_strategy_ban_policy",
]
