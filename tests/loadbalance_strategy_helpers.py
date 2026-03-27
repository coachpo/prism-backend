from __future__ import annotations

from itertools import count
from typing import Literal

from app.models.models import LoadbalanceStrategy

_strategy_counter = count(1)


def make_loadbalance_strategy(
    *,
    profile_id: int | None = None,
    profile: object | None = None,
    strategy_type: Literal[
        "single", "fill-first", "round-robin", "failover"
    ] = "single",
    failover_recovery_enabled: bool | None = None,
    name: str | None = None,
) -> LoadbalanceStrategy:
    if failover_recovery_enabled is None:
        failover_recovery_enabled = strategy_type != "single"

    payload: dict[str, object] = {
        "name": name or f"{strategy_type}-strategy-{next(_strategy_counter)}",
        "strategy_type": strategy_type,
        "failover_recovery_enabled": failover_recovery_enabled,
    }
    if profile is not None:
        payload["profile"] = profile
    elif profile_id is not None:
        payload["profile_id"] = profile_id
    else:
        raise ValueError("Either profile or profile_id is required")

    return LoadbalanceStrategy(**payload)


__all__ = ["make_loadbalance_strategy"]
