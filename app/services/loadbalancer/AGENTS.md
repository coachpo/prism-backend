# BACKEND LOADBALANCER PACKAGE KNOWLEDGE BASE

## OVERVIEW
`services/loadbalancer/` is the backend load-balancer package. It is intentionally split into explicit seams for strategy CRUD, planning, persistent current state, recovery mutation, event emission, and management-facing current-state or event queries.

## STRUCTURE
```
loadbalancer/
├── admin.py      # Management-facing current-state reset/list and event query facade
├── events.py     # Loadbalance event enqueue helpers
├── planner.py    # Model resolution and read-only attempt planning
├── recovery.py   # Failure/recovery/probe mutation helpers
├── state.py      # Persistent current-state reads and clears
├── strategies.py # Loadbalance strategy CRUD and strategy-scoped state clearing
├── types.py      # FailureKind, RecoveryStateEntry, AttemptPlan
└── __init__.py
```

## WHERE TO LOOK

- Read-only attempt planning and model resolution: `planner.py`
- Loadbalance strategy CRUD and delete guards: `strategies.py`
- Persistent routing-state queries and clears: `state.py`
- Recovery mutation and probe/recovery transitions: `recovery.py`
- Loadbalance event enqueue and payload shaping: `events.py`
- Management-facing current-state and event route surface: `admin.py`

## CONVENTIONS

- Keep attempt planning read-only. Planner returns `AttemptPlan` and does not mutate recovery state.
- Keep strategy CRUD and strategy-scoped state clearing in `strategies.py`.
- Keep persistent current-state reads and clears in `state.py`.
- Keep recovery mutation and event emission in `recovery.py` and `events.py`.
- Keep management routers on `admin.py` rather than importing low-level state/recovery/event helpers directly.

## ANTI-PATTERNS

- Do not reintroduce a flat `services/loadbalancer.py` barrel.
- Do not move probe-eligible claiming back into planner or request setup.
- Do not mix request logging or audit helpers with recovery mutation helpers.
