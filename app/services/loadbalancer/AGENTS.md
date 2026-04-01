# BACKEND LOADBALANCER PACKAGE KNOWLEDGE BASE

## OVERVIEW
`services/loadbalancer/` is the backend load-balancer package. It is intentionally split into explicit seams for strategy CRUD, planning, policy resolution, candidate scoring, deadline-aware execution, limiter state, runtime lease/state persistence, recovery mutation, event emission, and management-facing current-state or event queries.

## STRUCTURE
```
loadbalancer/
├── admin.py         # Management-facing current-state reset/list and event query facade
├── events.py        # Loadbalance event enqueue helpers
├── executor.py      # Deadline-aware attempt execution, hedging, commit/discard lifecycle
├── limiter.py       # Per-profile connection limiter state, lease acquisition, release, and reconciliation
├── planner.py       # Model resolution and read-only attempt planning
├── policy.py        # Effective strategy policy resolution and ban-policy normalization or validation
├── recovery.py      # Failure/recovery/probe mutation helpers
├── runtime_store.py # Runtime state upsert/lock, lease persistence, reconciliation, compaction
├── scoring.py       # Monitoring/circuit/saturation-aware candidate scoring
├── state.py         # Persistent current-state reads and clears for management surfaces
├── strategies.py    # Loadbalance strategy CRUD and strategy-scoped state clearing
├── types.py         # FailureKind, RecoveryStateEntry, AttemptPlan, runtime-store types
└── __init__.py
```

## WHERE TO LOOK

- Read-only attempt planning and model resolution: `planner.py`
- Monitoring/circuit/saturation-aware candidate ranking: `scoring.py`
- Deadline-aware attempt execution, hedge budget, and prepared-response commit/discard flow: `executor.py`
- Loadbalance strategy CRUD and delete guards: `strategies.py`
- Effective strategy policy and ban-policy normalization: `policy.py`
- Per-profile connection limiter state and lease lifecycle: `limiter.py`
- Runtime lease/state persistence, locking, reconciliation, and compaction: `runtime_store.py`
- Persistent routing-state queries and management clears: `state.py`
- Recovery mutation and probe/recovery transitions: `recovery.py`
- Loadbalance event enqueue and payload shaping: `events.py`
- Management-facing current-state and event route surface: `admin.py`

## CONVENTIONS

- Keep attempt planning read-only. Planner returns `AttemptPlan` and does not mutate recovery state.
- Keep scoring in `scoring.py`; circuit, latency, recent-failure, saturation, and stale-observation penalties should not leak into routers or planner helpers.
- Keep deadline-aware attempt orchestration in `executor.py`; accepted responses use its prepared commit/discard lifecycle.
- Keep policy resolution in `policy.py`. It owns effective-policy lookup and ban-policy normalization or validation.
- Keep limiter state in `limiter.py`. It owns per-profile connection leasing and reconciliation.
- Keep runtime lease/state persistence, locking, and compaction in `runtime_store.py`.
- Keep strategy CRUD and strategy-scoped state clearing in `strategies.py`.
- Keep persistent current-state reads and clears in `state.py`.
- Keep recovery mutation and event emission in `recovery.py` and `events.py`.
- Keep management routers on `admin.py` rather than importing low-level state/recovery/event helpers directly.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Prefer the best current implementation shape over preserving the old one. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS

- Do not reintroduce a flat `services/loadbalancer.py` barrel.
- Do not move probe-eligible claiming back into planner or request setup.
- Do not duplicate lease/state persistence or reconcile logic outside `runtime_store.py`.
- Do not duplicate scoring or deadline-aware execution logic outside `scoring.py` and `executor.py`.
- Do not mix request logging or audit helpers with recovery mutation helpers.
