# BACKEND TEST SERVICES KNOWLEDGE BASE

## OVERVIEW
`services/` holds focused backend tests that run against PostgreSQL through the shared testcontainer setup in `conftest.py`. This tree is the home for service-level coverage that does not belong in the smoke or multi-profile isolation hierarchies.

## STRUCTURE
```
services/
├── test_auth_hot_path_cache.py
├── test_background_tasks.py
├── test_crypto.py
├── test_loadbalance_strategies.py
├── test_loadbalancer_{executor,limiter,planner,recovery,runtime_store,scoring,state}.py
├── test_monitoring_{queries,routing_feedback,scheduler,settings}.py
├── test_stats_summary_request_logs.py
├── test_streaming_buffer.py
├── test_throughput_service.py
├── test_usage_{event_logging,snapshot_service}.py
└── test_webauthn_service.py
```

## WHERE TO LOOK

- Auth cache behavior that stays out of named-defect smoke coverage: `test_auth_hot_path_cache.py`
- Background worker and task-queue behavior: `test_background_tasks.py`
- Crypto helpers and service-level secret handling: `test_crypto.py`
- Loadbalance strategy CRUD plus loadbalancer planner, scoring, limiter, runtime-store, executor, recovery, and state behavior: `test_loadbalance_strategies.py`, `test_loadbalancer_*.py`
- Monitoring scheduler, routing-feedback, settings, and query behavior: `test_monitoring_*.py`
- Stats queries, summaries, request logs, usage events, and usage snapshots: `test_stats_summary_request_logs.py`, `test_usage_event_logging.py`, `test_usage_snapshot_service.py`
- Streaming helpers and pass-through behavior: `test_streaming_buffer.py`
- Throughput service behavior: `test_throughput_service.py`
- WebAuthn service behavior: `test_webauthn_service.py`

## SERVICE FACTS

- These tests stay outside `smoke_defect_regressions/` because they are service-focused, not named DEF regressions.
- These tests stay outside `multi_profile_isolation/` because their point is service behavior, not selected-profile versus active-profile containment.
- The tree exists so auth cache, background task, crypto, loadbalancer, monitoring, stats, usage-event, streaming, throughput, and WebAuthn coverage has one clear handoff instead of leaking into the other hierarchies.
- Keep PostgreSQL grounding explicit when adding or reading these cases, because they use the same backend testcontainer setup as the rest of `tests/`.

## CONVENTIONS

- Keep this tree for service behavior that is neither a DEF regression nor a cross-profile isolation guarantee.
- If a service test becomes a named defect or a profile-containment check, move it to the matching tree instead of duplicating it here.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Prefer the best current implementation shape over preserving the old one. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS

- Do not fold service tests into the smoke tree just because they touch a failure path.
- Do not fold service tests into the isolation tree just because they read profile-scoped data.
- Do not add another aggregator layer here.
