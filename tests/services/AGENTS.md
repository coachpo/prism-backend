# BACKEND TEST SERVICES KNOWLEDGE BASE

## OVERVIEW
`services/` holds focused backend tests that run against PostgreSQL through the shared testcontainer setup in `conftest.py`. This tree is the home for service-level coverage that does not belong in the smoke or multi-profile isolation hierarchies.

## STRUCTURE
```
services/
├── test_auth_hot_path_cache.py
├── test_background_tasks.py
├── test_crypto.py
├── test_loadbalancer_*.py
├── test_stats_*.py
├── test_streaming_*.py
├── test_throughput_service.py
└── test_webauthn_service.py
```

## WHERE TO LOOK

- Auth cache behavior that stays out of named-defect smoke coverage: `test_auth_hot_path_cache.py`
- Background worker and task-queue behavior: `test_background_tasks.py`
- Crypto helpers and service-level secret handling: `test_crypto.py`
- Loadbalancer service behavior that is broader than a single defect or a profile split: `test_loadbalancer_*.py`
- Stats queries, summaries, and request-log service behavior: `test_stats_*.py`
- Streaming helpers and pass-through behavior: `test_streaming_*.py`
- Throughput service behavior: `test_throughput_service.py`
- WebAuthn service behavior: `test_webauthn_service.py`

## SERVICE FACTS

- These tests stay outside `smoke_defect_regressions/` because they are service-focused, not named DEF regressions.
- These tests stay outside `multi_profile_isolation/` because their point is service behavior, not selected-profile versus active-profile containment.
- The tree exists so auth cache, background task, crypto, loadbalancer, stats, streaming, throughput, and WebAuthn coverage has one clear handoff instead of leaking into the other hierarchies.
- Keep PostgreSQL grounding explicit when adding or reading these cases, because they use the same backend testcontainer setup as the rest of `tests/`.

## CONVENTIONS

- Keep this tree for service behavior that is neither a DEF regression nor a cross-profile isolation guarantee.
- If a service test becomes a named defect or a profile-containment check, move it to the matching tree instead of duplicating it here.

## ANTI-PATTERNS

- Do not fold service tests into the smoke tree just because they touch a failure path.
- Do not fold service tests into the isolation tree just because they read profile-scoped data.
- Do not add another aggregator layer here.
