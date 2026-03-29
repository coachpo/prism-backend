from typing import TypedDict


class LoadbalanceEventSummaryPayload(TypedDict):
    event: str
    reason: str
    operation: str
    cooldown: str


_FAILURE_KIND_LABELS: dict[str, str] = {
    "transient_http": "transient HTTP failure",
    "connect_error": "connection error",
    "timeout": "timeout",
}


def _format_duration(seconds: float) -> str:
    normalized = max(seconds, 0.0)
    if normalized == 1.0:
        return "1 second"
    if normalized.is_integer():
        return f"{int(normalized)} seconds"
    return f"{normalized:.2f} seconds"


def describe_loadbalance_event(
    *,
    event_type: str,
    failure_kind: str | None,
    consecutive_failures: int,
    cooldown_seconds: float,
    failure_threshold: int | None,
) -> LoadbalanceEventSummaryPayload:
    failure_label = _FAILURE_KIND_LABELS.get(failure_kind or "", "failure")
    cooldown_label = _format_duration(cooldown_seconds)
    threshold_label = (
        f"the failover threshold of {failure_threshold}"
        if failure_threshold is not None
        else "the failover threshold"
    )

    if event_type == "max_cooldown_strike":
        return {
            "event": "Connection hit max open interval",
            "reason": (
                f"The {failure_label} pushed the connection to the configured maximum open interval after {consecutive_failures} consecutive failures."
            ),
            "operation": (
                "Prism recorded a max-open strike so operators can track whether the connection should escalate into a ban."
            ),
            "cooldown": cooldown_label,
        }

    if event_type == "banned":
        return {
            "event": "Connection was banned",
            "reason": (
                f"The {failure_label} reached the ban escalation threshold after {consecutive_failures} consecutive failures."
            ),
            "operation": (
                "Prism removed the connection from normal adaptive routing until the ban clears or an operator resets it."
            ),
            "cooldown": cooldown_label,
        }

    if event_type == "opened":
        reason = (
            f"The {failure_label} raised the streak to {consecutive_failures} consecutive failures, "
            f"meeting {threshold_label}."
        )
        return {
            "event": "Connection opened its circuit",
            "reason": reason,
            "operation": (
                f"Prism opened the circuit for {cooldown_label} before the connection becomes eligible for another probe attempt."
            ),
            "cooldown": cooldown_label,
        }

    if event_type == "extended":
        reason = (
            f"Another {failure_label} happened before the active cooldown finished, and the streak is now "
            f"{consecutive_failures} consecutive failures."
        )
        return {
            "event": "Circuit open interval was extended",
            "reason": reason,
            "operation": (
                f"Prism kept the circuit open and restarted the recovery timer for {cooldown_label}."
            ),
            "cooldown": cooldown_label,
        }

    if event_type == "probe_eligible":
        return {
            "event": "Connection became probe eligible",
            "reason": (
                f"The open interval after the last {failure_label} completed, so the connection can be checked again."
            ),
            "operation": (
                "Prism can let this connection receive another probe or traffic attempt to confirm whether it recovered."
            ),
            "cooldown": f"{cooldown_label} open interval completed",
        }

    if event_type == "recovered":
        return {
            "event": "Connection recovered",
            "reason": (
                f"The connection was marked healthy again after the last {failure_label}."
            ),
            "operation": (
                "Prism closed the circuit and returned the connection to normal adaptive routing."
            ),
            "cooldown": f"Recovered after a {cooldown_label} open interval",
        }

    return {
        "event": "Failure was recorded",
        "reason": (
            f"The {failure_label} raised the streak to {consecutive_failures} consecutive failures, "
            f"which is still below {threshold_label}."
        ),
        "operation": (
            "Prism kept the connection available and only updated the runtime failure streak, so no circuit-open interval started."
        ),
        "cooldown": "No open interval started",
    }


__all__ = ["describe_loadbalance_event"]
