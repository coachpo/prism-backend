import logging
from typing import Literal, TypedDict

from app.core.config import get_settings

LOGGER_NAME = "app.services.loadbalancer"
logger = logging.getLogger(LOGGER_NAME)

FailureKind = Literal["transient_http", "auth_like", "connect_error", "timeout"]


class RecoveryStateEntry(TypedDict):
    consecutive_failures: int
    blocked_until_mono: float | None
    last_cooldown_seconds: float
    last_failure_kind: FailureKind | None
    probe_eligible_logged: bool


_recovery_state: dict[tuple[int, int], RecoveryStateEntry] = {}


def get_loadbalancer_settings():
    return get_settings()


__all__ = [
    "FailureKind",
    "LOGGER_NAME",
    "RecoveryStateEntry",
    "_recovery_state",
    "get_loadbalancer_settings",
    "logger",
]
