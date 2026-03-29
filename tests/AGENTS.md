# BACKEND TEST SUITE KNOWLEDGE BASE

## OVERVIEW
`tests/` is the backend regression suite. It runs against PostgreSQL and centers its top-level shape on smoke and multi-profile aggregators.

## STRUCTURE
```text
tests/
├── conftest.py
├── loadbalance_strategy_helpers.py
├── test_backend_version_metadata.py
├── test_smoke_defect_regressions.py
├── test_multi_profile_isolation.py
├── test_realtime_broadcast.py
├── services/AGENTS.md
├── multi_profile_isolation/AGENTS.md
└── smoke_defect_regressions/
    ├── AGENTS.md
    ├── test_config_cases/AGENTS.md
    ├── test_costing_cases/AGENTS.md
    ├── test_proxy_cases/AGENTS.md
    └── test_startup_cases/AGENTS.md
```

## CHILD DOCS
- `smoke_defect_regressions/AGENTS.md`: DEF hierarchy map and aggregator expectations.
- `smoke_defect_regressions/test_config_cases/AGENTS.md`: focused config smoke cluster.
- `smoke_defect_regressions/test_costing_cases/AGENTS.md`: focused costing smoke cluster.
- `smoke_defect_regressions/test_proxy_cases/AGENTS.md`: focused proxy smoke cluster.
- `smoke_defect_regressions/test_startup_cases/AGENTS.md`: focused startup/auth/schema smoke cluster.
- `multi_profile_isolation/AGENTS.md`: profile-isolation subtree and non-re-exported containment leaves.
- `services/AGENTS.md`: focused backend service-test coverage outside smoke and isolation trees.

## WHERE TO LOOK
- PostgreSQL bootstrap: `conftest.py`
- Smoke export surface: `test_smoke_defect_regressions.py`
- Multi-profile isolation: `test_multi_profile_isolation.py`
- Realtime websocket behavior: `test_realtime_broadcast.py`
- Focused service coverage: `services/AGENTS.md`
- Focused smoke clusters: `smoke_defect_regressions/test_config_cases/AGENTS.md`, `smoke_defect_regressions/test_costing_cases/AGENTS.md`, `smoke_defect_regressions/test_proxy_cases/AGENTS.md`, `smoke_defect_regressions/test_startup_cases/AGENTS.md`

## TEST FACTS
- `test_smoke_defect_regressions.py` is the top-level DEF aggregator and now includes grouped config, costing, startup, and proxy leaves.
- `test_multi_profile_isolation.py` is the top-level aggregator for lifecycle, scoping, runtime, observability, and config export or import isolation.
- `test_backend_version_metadata.py` covers the root/backend version contract outside the smoke and isolation hierarchies.
- `services/` holds focused backend tests that do not fit the smoke or isolation hierarchies.
- These tests are grounded in PostgreSQL semantics through the shared testcontainer bootstrap in `conftest.py`.

## CONVENTIONS
- Run backend tests from `backend/` through `uv run`.
- Keep top-level aggregators current when adding smoke leaves, and update the multi-profile top-level aggregator when a subtree leaf is meant to participate in that re-export surface.
- Put defect-numbered regressions under `smoke_defect_regressions/` and cross-profile guarantees under `multi_profile_isolation/`.
- Keep service or realtime tests outside those hierarchies when they do not map cleanly to a DEF or profile-isolation concern.

## ANTI-PATTERNS
- Do not assume SQLite-like behavior. This suite is grounded in PostgreSQL semantics.
- Do not add a new smoke leaf without wiring the top-level aggregator, and do not claim a multi-profile leaf is top-level aggregated unless `test_multi_profile_isolation.py` actually re-exports it.
- Do not invent extra hierarchy docs under the existing smoke or isolation subtrees when the parent coverage already explains the shape. The config, costing, proxy, and startup smoke leaves are the current justified exceptions.
