# BACKEND LOADBALANCER SUPPORT KNOWLEDGE BASE

## OVERVIEW
`services/loadbalancer_support/` is the split package behind `../loadbalancer.py`: model-and-connection queries, active-attempt planning, in-memory recovery state, and failed/recovered connection transitions.

## STRUCTURE
```
loadbalancer_support/
├── attempts.py   # Active-connection filtering and attempt-plan building
├── events.py     # Internal loadbalance-event scheduling helpers
├── queries.py    # Model config + connection eager loading
├── recovery.py   # Mark failed/recovered transitions against shared state
└── state.py      # FailureKind, RecoveryStateEntry, shared recovery-state store
```

## WHERE TO LOOK

- Public re-export boundary: `../loadbalancer.py`, `__init__.py`
- Attempt ordering and active-connection selection: `attempts.py`
- Model-with-connections fetches: `queries.py`
- Failure/recovery mutation paths: `recovery.py`
- Shared recovery-state structures: `state.py`
- Persistent event handoff: `events.py`

## CONVENTIONS

- Treat `../loadbalancer.py` as the public boundary; most callers should not import leaf modules directly.
- Keep recovery-state reads and writes centralized in `state.py` and `recovery.py`.
- Keep model+connection query shaping in `queries.py` so proxy/runtime paths do not re-build that eager-loading logic.
- Keep attempt planning in `attempts.py`; callers should consume the plan rather than re-deriving priority order ad hoc.
- `events.py` is an internal helper for persistent loadbalance-event recording; recovery logic should use it instead of open-coding event tasks.

## ANTI-PATTERNS

- Do not mutate `_recovery_state` directly from callers outside this package.
- Do not duplicate active-connection filtering or attempt ordering in routers or services.
- Do not record loadbalance transitions from unrelated modules when the recovery helpers already own that side effect.
