from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Literal, TypedDict

from app.models.models import Connection
from app.services.loadbalancer.policy import (
    BanMode,
    EffectiveLoadbalancePolicy,
    resolve_effective_loadbalance_policy,
)

FailureKind = Literal["transient_http", "connect_error", "timeout"]
RuntimeCircuitState = Literal["closed", "open", "half_open"]
RuntimeLeaseKind = Literal["stream", "non_stream", "half_open_probe"]
RuntimeLeaseDenyReason = Literal[
    "qps_limit",
    "in_flight_limit",
    "probe_not_ready",
    "probe_in_progress",
]


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
class RuntimeLeaseAcquireResult:
    admitted: bool
    lease_token: str | None = None
    deny_reason: RuntimeLeaseDenyReason | None = None


class RuntimeReconcileSummary(TypedDict):
    expired_leases_released: int
    state_rows_deleted: int
    state_rows_updated: int


@dataclass(frozen=True, slots=True)
class AttemptCandidateScoreInput:
    connection: Connection
    circuit_state: RuntimeCircuitState
    blocked_until_at: datetime | None
    banned_until_at: datetime | None
    probe_available_at: datetime | None
    in_flight_non_stream: int
    in_flight_stream: int
    qps_window_count: int
    live_p95_latency_ms: float | None
    last_live_failure_kind: str | None
    last_live_failure_at: datetime | None
    last_live_success_at: datetime | None
    last_probe_status: str | None
    last_probe_at: datetime | None
    endpoint_ping_ewma_ms: float | None
    conversation_delay_ewma_ms: float | None


@dataclass(frozen=True, slots=True)
class AttemptCandidate:
    connection: Connection
    score_input: AttemptCandidateScoreInput
    score: float
    sort_key: tuple[float, int, int]


@dataclass(frozen=True, slots=True, init=False)
class AttemptPlan:
    policy: EffectiveLoadbalancePolicy
    candidates: list[AttemptCandidate]
    blocked_connection_ids: list[int]
    probe_eligible_connection_ids: list[int]

    def __init__(
        self,
        *,
        policy: EffectiveLoadbalancePolicy | None = None,
        candidates: list[AttemptCandidate] | None = None,
        connections: list[Connection] | None = None,
        blocked_connection_ids: list[int] | None = None,
        probe_eligible_connection_ids: list[int] | None = None,
    ) -> None:
        resolved_policy = policy or resolve_effective_loadbalance_policy(
            SimpleNamespace(routing_policy={"kind": "adaptive"})
        )
        resolved_candidates = candidates
        if resolved_candidates is None:
            resolved_candidates = [
                AttemptCandidate(
                    connection=connection,
                    score_input=AttemptCandidateScoreInput(
                        connection=connection,
                        circuit_state="closed",
                        blocked_until_at=None,
                        banned_until_at=None,
                        probe_available_at=None,
                        in_flight_non_stream=0,
                        in_flight_stream=0,
                        qps_window_count=0,
                        live_p95_latency_ms=None,
                        last_live_failure_kind=None,
                        last_live_failure_at=None,
                        last_live_success_at=None,
                        last_probe_status=None,
                        last_probe_at=None,
                        endpoint_ping_ewma_ms=None,
                        conversation_delay_ewma_ms=None,
                    ),
                    score=0.0,
                    sort_key=(0.0, getattr(connection, "priority", 0), connection.id),
                )
                for connection in (connections or [])
            ]
        object.__setattr__(self, "policy", resolved_policy)
        object.__setattr__(self, "candidates", resolved_candidates)
        object.__setattr__(
            self,
            "blocked_connection_ids",
            blocked_connection_ids or [],
        )
        object.__setattr__(
            self,
            "probe_eligible_connection_ids",
            probe_eligible_connection_ids or [],
        )

    @property
    def connections(self) -> list[Connection]:
        return [candidate.connection for candidate in self.candidates]


__all__ = [
    "AttemptCandidate",
    "AttemptCandidateScoreInput",
    "AttemptPlan",
    "FailureKind",
    "RecoveryStateEntry",
    "RuntimeCircuitState",
    "RuntimeLeaseAcquireResult",
    "RuntimeLeaseDenyReason",
    "RuntimeLeaseKind",
    "RuntimeReconcileSummary",
]
