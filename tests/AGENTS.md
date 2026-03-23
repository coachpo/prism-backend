# BACKEND TEST SUITE KNOWLEDGE BASE

## OVERVIEW
`tests/` is the backend regression suite. It runs against PostgreSQL through the testcontainer setup in `conftest.py`, centers its high-level shape on the top-level smoke and isolation aggregators, and keeps extra focused coverage for realtime and service boundaries.

## STRUCTURE
```
tests/
├── conftest.py                           # PostgreSQL testcontainer and Alembic bootstrap
├── test_smoke_defect_regressions.py      # Top-level DEF aggregator, currently through DEF078
├── test_multi_profile_isolation.py       # Top-level selected-vs-active profile isolation aggregator
├── test_realtime_broadcast.py            # Websocket channel fanout and dashboard update coverage
├── services/                             # Focused service coverage outside the main hierarchies
├── smoke_defect_regressions/
│   └── AGENTS.md                         # DEF-domain map and aggregator expectations
└── multi_profile_isolation/
    └── AGENTS.md                         # Isolation-domain map, including config export or import containment
```

## WHERE TO LOOK

- Test container setup and migrated database bootstrap: `conftest.py`
- Smoke regression export surface and current DEF range: `test_smoke_defect_regressions.py`
- Multi-profile export surface: `test_multi_profile_isolation.py`
- Realtime websocket and `dashboard.update` behavior: `test_realtime_broadcast.py`
- Service-level coverage: `services/test_background_tasks.py`, `services/test_webauthn_service.py`, `services/test_stats_summary_request_logs.py`, `services/test_throughput_service.py`, `services/test_crypto.py`, `services/test_auth_hot_path_cache.py`
- DEF smoke hierarchy: `smoke_defect_regressions/AGENTS.md`, `smoke_defect_regressions/`
- Multi-profile hierarchy, including profile-scoped config export or import isolation: `multi_profile_isolation/AGENTS.md`, `multi_profile_isolation/`

## TEST FACTS

- `test_smoke_defect_regressions.py` is the top-level aggregator for DEF regressions and currently exports cases through `TestDEF078_ObservabilityMigrationTogglesUnloggedPersistence`.
- `test_multi_profile_isolation.py` is the top-level aggregator for lifecycle, scoping, runtime, observability, and config export or import isolation.
- `services/` holds focused tests that don't belong in the DEF or isolation hierarchies.
- Parent coverage for the smoke and isolation trees lives in the two child AGENTS files. Don't add new AGENTS docs beneath those trees for the current structure.

## COMMANDS

```bash
uv sync --locked
uv run pytest tests/ -v
uv run pytest tests/test_smoke_defect_regressions.py -v
uv run pytest tests/test_multi_profile_isolation.py -v
```

## CONVENTIONS

- Run backend tests from `backend/` through `uv run`.
- Keep top-level aggregators up to date when adding new smoke or isolation leaf files.
- Put defect-numbered regressions under `smoke_defect_regressions/` and cross-profile guarantees under `multi_profile_isolation/`.
- Keep service or realtime tests outside those hierarchies when they don't map cleanly to a DEF or profile-isolation concern.

## ANTI-PATTERNS

- Do not assume SQLite-like behavior. This suite is grounded in PostgreSQL semantics.
- Do not add a new smoke or isolation leaf without wiring the relevant top-level aggregator.
- Do not invent extra hierarchy docs under the existing smoke or isolation subtrees when the parent coverage already explains the shape.
