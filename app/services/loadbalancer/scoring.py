from __future__ import annotations

from datetime import datetime

from app.core.time import ensure_utc_datetime, utc_now

from .policy import EffectiveLoadbalancePolicy
from .types import AttemptCandidate, AttemptCandidateScoreInput

_OPEN_CIRCUIT_PENALTY = 4_000.0
_HALF_OPEN_CIRCUIT_PENALTY = 2_000.0
_STALE_STATE_PENALTY = 6_000.0
_UNHEALTHY_PROBE_PENALTY = 10_000.0
_RECENT_FAILURE_BASE_PENALTY = 8_000.0
_SATURATION_BASE_PENALTY = 10_000.0


def _connection_priority(score_input: AttemptCandidateScoreInput) -> int:
    value = getattr(score_input.connection, "priority", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _connection_id(score_input: AttemptCandidateScoreInput) -> int:
    value = getattr(score_input.connection, "id", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _ratio(current: int, limit: object) -> float:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return 0.0
    return max(float(current) / float(limit), 0.0)


def _objective_latency_multiplier(policy: EffectiveLoadbalancePolicy) -> float:
    return 1.0 if policy.routing_objective == "minimize_latency" else 0.35


def _objective_failure_multiplier(policy: EffectiveLoadbalancePolicy) -> float:
    return 1.0 if policy.routing_objective == "minimize_latency" else 1.75


def _objective_saturation_multiplier(policy: EffectiveLoadbalancePolicy) -> float:
    return 1.0 if policy.routing_objective == "minimize_latency" else 1.25


def _latest_observation_at(score_input: AttemptCandidateScoreInput) -> datetime | None:
    observed_at_values = [
        ensure_utc_datetime(score_input.last_probe_at),
        ensure_utc_datetime(score_input.last_live_failure_at),
        ensure_utc_datetime(score_input.last_live_success_at),
    ]
    present = [value for value in observed_at_values if value is not None]
    if not present:
        return None
    return max(present)


def _is_stale(
    policy: EffectiveLoadbalancePolicy,
    score_input: AttemptCandidateScoreInput,
    *,
    now_at: datetime,
) -> bool:
    latest_observation_at = _latest_observation_at(score_input)
    if latest_observation_at is None:
        return True
    age_seconds = (now_at - latest_observation_at).total_seconds()
    return age_seconds > float(policy.monitoring_stale_after_seconds)


def _circuit_penalty(score_input: AttemptCandidateScoreInput) -> float:
    if score_input.circuit_state == "open":
        return _OPEN_CIRCUIT_PENALTY
    if score_input.circuit_state == "half_open":
        return _HALF_OPEN_CIRCUIT_PENALTY
    return 0.0


def _saturation_penalty(
    policy: EffectiveLoadbalancePolicy,
    score_input: AttemptCandidateScoreInput,
    *,
    is_streaming: bool,
) -> float:
    qps_limit = getattr(score_input.connection, "qps_limit", None)
    in_flight_limit = getattr(
        score_input.connection,
        "max_in_flight_stream" if is_streaming else "max_in_flight_non_stream",
        None,
    )
    in_flight_current = (
        score_input.in_flight_stream
        if is_streaming
        else score_input.in_flight_non_stream
    )
    saturation_ratio = _ratio(score_input.qps_window_count, qps_limit) + _ratio(
        in_flight_current,
        in_flight_limit,
    )
    return (
        _objective_saturation_multiplier(policy)
        * _SATURATION_BASE_PENALTY
        * saturation_ratio
    )


def _latency_penalty(
    policy: EffectiveLoadbalancePolicy,
    score_input: AttemptCandidateScoreInput,
    *,
    stale: bool,
) -> float:
    latency_penalty = float(score_input.live_p95_latency_ms or 0.0)
    if policy.monitoring_enabled and not stale:
        if score_input.endpoint_ping_ewma_ms is not None:
            latency_penalty += policy.monitoring_endpoint_ping_weight * float(
                score_input.endpoint_ping_ewma_ms
            )
        if score_input.conversation_delay_ewma_ms is not None:
            latency_penalty += policy.monitoring_conversation_delay_weight * float(
                score_input.conversation_delay_ewma_ms
            )
    return _objective_latency_multiplier(policy) * latency_penalty


def _recent_failure_penalty(
    policy: EffectiveLoadbalancePolicy,
    score_input: AttemptCandidateScoreInput,
    *,
    now_at: datetime,
) -> float:
    failure_at = ensure_utc_datetime(score_input.last_live_failure_at)
    success_at = ensure_utc_datetime(score_input.last_live_success_at)
    if failure_at is None:
        return 0.0
    if success_at is not None and success_at > failure_at:
        return 0.0

    age_seconds = max((now_at - failure_at).total_seconds(), 0.0)
    freshness_window = max(float(policy.monitoring_stale_after_seconds), 1.0)
    freshness_ratio = max(0.0, 1.0 - (age_seconds / freshness_window))
    return (
        _objective_failure_multiplier(policy)
        * policy.monitoring_failure_penalty_weight
        * _RECENT_FAILURE_BASE_PENALTY
        * freshness_ratio
    )


def _probe_penalty(
    policy: EffectiveLoadbalancePolicy,
    score_input: AttemptCandidateScoreInput,
    *,
    stale: bool,
) -> float:
    if not policy.monitoring_enabled:
        return 0.0
    if stale:
        return _objective_failure_multiplier(policy) * _STALE_STATE_PENALTY
    if score_input.last_probe_status == "unhealthy":
        return (
            _objective_failure_multiplier(policy)
            * policy.monitoring_failure_penalty_weight
            * _UNHEALTHY_PROBE_PENALTY
        )
    return 0.0


def score_candidate(
    policy: EffectiveLoadbalancePolicy,
    score_input: AttemptCandidateScoreInput,
    *,
    now_at: datetime | None = None,
    is_streaming: bool = False,
) -> float:
    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    stale = _is_stale(policy, score_input, now_at=normalized_now)
    total_score = 0.0
    total_score += _circuit_penalty(score_input)
    total_score += _saturation_penalty(
        policy,
        score_input,
        is_streaming=is_streaming,
    )
    total_score += _latency_penalty(policy, score_input, stale=stale)
    total_score += _recent_failure_penalty(
        policy,
        score_input,
        now_at=normalized_now,
    )
    total_score += _probe_penalty(policy, score_input, stale=stale)
    return total_score


def candidate_sort_key(
    policy: EffectiveLoadbalancePolicy,
    score_input: AttemptCandidateScoreInput,
    *,
    now_at: datetime | None = None,
    is_streaming: bool = False,
) -> tuple[float, int, int]:
    score = round(
        score_candidate(
            policy,
            score_input,
            now_at=now_at,
            is_streaming=is_streaming,
        ),
        6,
    )
    return (score, _connection_priority(score_input), _connection_id(score_input))


def rank_candidates(
    *,
    policy: EffectiveLoadbalancePolicy,
    candidate_inputs: list[AttemptCandidateScoreInput],
    now_at: datetime | None = None,
    is_streaming: bool = False,
) -> list[AttemptCandidate]:
    normalized_now = ensure_utc_datetime(now_at) or utc_now()
    ranked_candidates = [
        AttemptCandidate(
            connection=score_input.connection,
            score_input=score_input,
            score=score_candidate(
                policy,
                score_input,
                now_at=normalized_now,
                is_streaming=is_streaming,
            ),
            sort_key=candidate_sort_key(
                policy,
                score_input,
                now_at=normalized_now,
                is_streaming=is_streaming,
            ),
        )
        for score_input in candidate_inputs
    ]
    return sorted(ranked_candidates, key=lambda candidate: candidate.sort_key)


__all__ = [
    "candidate_sort_key",
    "rank_candidates",
    "score_candidate",
]
