from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypedDict

from app.models.models import Connection
from app.services.loadbalancer.policy import BanMode

FailureKind = Literal["transient_http", "connect_error", "timeout"]


class RecoveryStateEntry(TypedDict):
    consecutive_failures: int
    blocked_until_at: datetime | None
    max_cooldown_strikes: int
    ban_mode: BanMode
    banned_until_at: datetime | None
    last_cooldown_seconds: float
    last_failure_kind: FailureKind | None
    probe_eligible_logged: bool


@dataclass(frozen=True, slots=True)
class AttemptPlan:
    connections: list[Connection]
    blocked_connection_ids: list[int]
    probe_eligible_connection_ids: list[int]


__all__ = ["AttemptPlan", "FailureKind", "RecoveryStateEntry"]
