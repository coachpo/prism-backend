# BACKEND TEST SUITE KNOWLEDGE BASE

## OVERVIEW
`tests/` is the backend regression suite. It runs against PostgreSQL through the testcontainer setup in `conftest.py` and centers its top-level shape on the smoke and multi-profile aggregators, with focused service coverage kept in `services/`.

## STRUCTURE
```
tests/
├── conftest.py                           # PostgreSQL testcontainer and Alembic bootstrap
├── loadbalance_strategy_helpers.py       # Shared loadbalance strategy fixtures and payload helpers
├── test_backend_version_metadata.py      # Root version metadata contract coverage
├── test_smoke_defect_regressions.py      # Top-level DEF aggregator, currently through DEF087
├── test_multi_profile_isolation.py       # Top-level selected-vs-active profile isolation aggregator for the core subtree
├── test_realtime_broadcast.py            # Websocket channel fanout and dashboard update coverage
├── services/
│   └── AGENTS.md                         # Focused service coverage outside smoke and isolation trees
├── smoke_defect_regressions/
│   ├── AGENTS.md                         # DEF-domain map and aggregator expectations
│   ├── test_proxy_cases/AGENTS.md        # Focused proxy smoke regression cluster
│   └── test_startup_cases/AGENTS.md      # Focused startup/auth/migration smoke regression cluster
└── multi_profile_isolation/
    └── AGENTS.md                         # Isolation-domain map, including config export and import containment
```

## CHILD DOCS

- `smoke_defect_regressions/AGENTS.md`: DEF hierarchy map, leaf ownership, and aggregator expectations.
- `smoke_defect_regressions/test_proxy_cases/AGENTS.md`: focused proxy smoke regression cluster inside the DEF tree.
- `smoke_defect_regressions/test_startup_cases/AGENTS.md`: focused startup/auth/migration smoke regression cluster inside the DEF tree.
- `multi_profile_isolation/AGENTS.md`: profile-isolation hierarchy map, cross-profile containment expectations, and the non-re-exported `test_connection_priority_isolation.py` leaf.
- `services/AGENTS.md`: focused service-test handoff for auth cache, background tasks, crypto, loadbalancer, stats, streaming, throughput, and WebAuthn coverage.

## WHERE TO LOOK

- Testcontainer setup and migrated database bootstrap: `conftest.py`
- Shared loadbalance strategy helper payloads used by focused tests: `loadbalance_strategy_helpers.py`
- Backend version metadata contract checks: `test_backend_version_metadata.py`
- Smoke regression export surface and current DEF range: `test_smoke_defect_regressions.py`
- Focused proxy smoke cluster inside the DEF tree: `smoke_defect_regressions/test_proxy_cases/AGENTS.md`
- Focused startup/auth/migration smoke cluster inside the DEF tree: `smoke_defect_regressions/test_startup_cases/AGENTS.md`
- Multi-profile export surface: `test_multi_profile_isolation.py`
- Multi-profile leaf that is currently not re-exported by the top-level aggregator: `multi_profile_isolation/test_connection_priority_isolation.py`
- Realtime websocket and `dashboard.update` behavior: `test_realtime_broadcast.py`
- Focused service coverage outside the two main trees: `services/`

## TEST FACTS

- `test_smoke_defect_regressions.py` is the top-level DEF aggregator and now includes two `DEF087` leaves (`HealthEndpointContract` on the startup side and `ProxyUnroutableTargetRejection` on the proxy side) alongside recent proxy-side `DEF081` and `DEF083` coverage.
- `test_multi_profile_isolation.py` is the top-level aggregator for lifecycle, scoping, runtime, observability, and config export or import isolation.
- `multi_profile_isolation/test_connection_priority_isolation.py` currently remains a direct subtree leaf instead of a top-level re-export in `test_multi_profile_isolation.py`.
- `services/` holds focused backend tests that do not fit the smoke or isolation hierarchies.
- `smoke_defect_regressions/test_startup_cases/` is now dense enough to justify its own child AGENTS file alongside the older proxy smoke leaf.
- `test_backend_version_metadata.py` covers the root/backend version contract outside the smoke and isolation hierarchies.
- These tests are grounded in PostgreSQL semantics. Parent docs should keep that fact visible and leave leaf-level scenario detail to the two child AGENTS files.

## CONVENTIONS

- Run backend tests from `backend/` through `uv run`.
- Keep top-level aggregators current when adding smoke leaves, and update the multi-profile top-level aggregator when a subtree leaf is meant to participate in that re-export surface.
- Put defect-numbered regressions under `smoke_defect_regressions/` and cross-profile guarantees under `multi_profile_isolation/`.
- Keep service or realtime tests outside those hierarchies when they do not map cleanly to a DEF or profile-isolation concern.
- Send focused service tests to `services/AGENTS.md` so the leaf docs stay separate from smoke and isolation coverage.

## ANTI-PATTERNS

- Do not assume SQLite-like behavior. This suite is grounded in PostgreSQL semantics.
- Do not add a new smoke leaf without wiring the top-level aggregator, and do not claim a multi-profile leaf is top-level aggregated unless `test_multi_profile_isolation.py` actually re-exports it.
- Do not invent extra hierarchy docs under the existing smoke or isolation subtrees when the parent coverage already explains the shape. `smoke_defect_regressions/test_proxy_cases/AGENTS.md` and `smoke_defect_regressions/test_startup_cases/AGENTS.md` are the current justified exceptions.
