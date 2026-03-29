# BACKEND SMOKE DEFECT REGRESSIONS KNOWLEDGE BASE

## OVERVIEW
`smoke_defect_regressions/` is the named-defect regression corpus. It groups DEF cases by concern, then re-exports them through `../test_smoke_defect_regressions.py`. The current top-level range reaches DEF087.

## STRUCTURE
```
smoke_defect_regressions/
├── test_proxy_cases/                 # Routing, failover, streaming, model or path guards + local AGENTS leaf
├── test_config_cases/                # Config export or import, schema validation, user-settings seed cases
├── test_costing_cases/               # Token parsing, special-token rules, pricing template CAS
├── test_startup_cases/               # Auth, CORS, batch delete, model health, schema, and startup contracts
│   └── AGENTS.md                     # Dense startup/auth/schema regression cluster
├── test_proxy.py                     # Proxy-domain aggregator
├── test_config.py                    # Config-domain aggregator
├── test_costing.py                   # Costing-domain aggregator
├── test_startup.py                   # Startup-domain aggregator
├── test_failover.py                  # Standalone failover recovery regression
├── test_headers.py                   # Header blocklist regression
├── test_connection_priority.py       # Connection priority regression
├── test_conditional_decompression.py # Conditional decompression regression
└── test_endpoint_ordering.py         # Endpoint ordering regression
```

## WHERE TO LOOK

- Top-level export surface and full DEF list through DEF087: `../test_smoke_defect_regressions.py`
- Proxy-domain aggregators and leaf cases: `test_proxy.py`, `test_proxy_cases/`, `test_proxy_cases/AGENTS.md`
- Config-domain aggregators and leaf cases: `test_config.py`, `test_config_cases/`
- Costing-domain aggregators and leaf cases: `test_costing.py`, `test_costing_cases/`
- Startup-domain aggregators and leaf cases: `test_startup.py`, `test_startup_cases/`
- Focused startup/auth/schema leaf map: `test_startup_cases/AGENTS.md`
- Standalone guards outside the grouped folders: `test_failover.py`, `test_headers.py`, `test_connection_priority.py`, `test_conditional_decompression.py`, `test_endpoint_ordering.py`
- Startup auth and proxy-key management flows: `test_startup_cases/auth_management_flows_tests.py`, `test_startup_cases/auth_proxy_key_generation_tests.py`
- Startup schema and observability guards: `test_startup_cases/loadbalance_profile_primary_key_tests.py`, `test_startup_cases/observability_storage_mode_tests.py`

## DEF FACTS

- The current top-level smoke aggregator includes two `DEF087` leaves: startup-side `TestDEF087_HealthEndpointContract` and proxy-side `TestDEF087_ProxyUnroutableTargetRejection`, alongside nearby proxy additions `DEF081` and `DEF083`.
- Startup cases are split by explicit concern file names, including auth management, proxy-key generation, CORS preflight auth bypass, health contract checks, loadbalance schema integrity, observability storage mode, profile scope or model health eager loading, and stats or batch-delete or endpoint-mapping behavior.
- The grouped folders already provide the parent structure for their leaf files unless a folder has grown enough to justify its own leaf map.
- `test_connection_priority.py` remains a standalone smoke leaf outside the top-level smoke re-export surface.

## BOUNDARY NOTES

- Keep PostgreSQL grounding explicit here, because these DEF regressions exercise the same testcontainer-backed database semantics as the rest of `tests/`.
- Keep smoke coverage focused on named defects and the re-export surface. Move auth cache, background task, crypto, loadbalancer, stats, streaming, throughput, and WebAuthn service cases to `../services/AGENTS.md` instead of growing this tree.

## FOLDER NOTES

- `test_proxy_cases/` now justifies its own leaf doc because it is a topic-specific proxy lookup cluster spanning health classification, logging/auth paths, model-update invariants, runtime target selection, and recovery or streaming behavior.
- `test_startup_cases/` now justifies its own leaf doc because it has grown into the densest startup-side cluster, spanning auth flows, middleware, health-contract checks, schema guards, usage-snapshot storage checks, and vendor/schema regressions.

## CONVENTIONS

- Keep new DEF classes uniquely numbered with `TestDEF###_*` naming.
- Add new leaf tests to the right concern folder first, then wire the matching domain aggregator and the top-level smoke aggregator when needed.
- Keep file names explicit about the guarded behavior so the regression map stays readable.

## ANTI-PATTERNS

- Do not reuse DEF numbers or insert a new regression into the wrong concern folder just because it is nearby.
- Do not stop at a leaf test file and forget the domain or top-level re-export path.
- Do not hide startup, auth, or observability regressions behind vague file names.
