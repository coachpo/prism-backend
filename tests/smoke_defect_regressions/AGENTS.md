# BACKEND SMOKE DEFECT REGRESSIONS KNOWLEDGE BASE

## OVERVIEW
`smoke_defect_regressions/` is the named-defect regression corpus. It groups DEF cases by concern, then re-exports them through `../test_smoke_defect_regressions.py`. The current top-level range reaches DEF079.

## STRUCTURE
```
smoke_defect_regressions/
├── test_proxy_cases/                 # Routing, failover, streaming, model or path guards
├── test_config_cases/                # Config export or import, schema validation, user-settings seed cases
├── test_costing_cases/               # Token parsing, special-token rules, pricing template CAS
├── test_startup_cases/               # Auth, CORS, batch delete, model health, migrations, startup contracts
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

- Top-level export surface and full DEF list through DEF079: `../test_smoke_defect_regressions.py`
- Proxy-domain aggregators and leaf cases: `test_proxy.py`, `test_proxy_cases/`
- Config-domain aggregators and leaf cases: `test_config.py`, `test_config_cases/`
- Costing-domain aggregators and leaf cases: `test_costing.py`, `test_costing_cases/`
- Startup-domain aggregators and leaf cases: `test_startup.py`, `test_startup_cases/`
- Standalone guards outside the grouped folders: `test_failover.py`, `test_headers.py`, `test_connection_priority.py`, `test_conditional_decompression.py`, `test_endpoint_ordering.py`
- Startup auth and proxy-key management flows: `test_startup_cases/auth_management_flows_tests.py`, `test_startup_cases/auth_proxy_key_generation_tests.py`
- Startup migration and observability guards: `test_startup_cases/loadbalance_migration_profile_pk_repair_tests.py`, `test_startup_cases/observability_unlogged_migration_tests.py`

## DEF FACTS

- The current top-level smoke aggregator exports DEF cases through `TestDEF079_ProxyApiKeyMetadataManagement`.
- Startup cases are split by explicit concern file names, including auth management, proxy-key generation, CORS preflight auth bypass, loadbalance migration repair, observability migration, profile scope or model health eager loading, and stats or batch-delete or endpoint-mapping behavior.
- The grouped folders already provide the parent structure for their leaf files. Don't add extra AGENTS docs under `test_proxy_cases/`, `test_config_cases/`, `test_costing_cases/`, or `test_startup_cases/` for the current layout.

## CONVENTIONS

- Keep new DEF classes uniquely numbered with `TestDEF###_*` naming.
- Add new leaf tests to the right concern folder first, then wire the matching domain aggregator and the top-level smoke aggregator when needed.
- Keep file names explicit about the guarded behavior so the regression map stays readable.

## ANTI-PATTERNS

- Do not reuse old DEF numbers or insert a new regression into the wrong concern folder just because it is nearby.
- Do not stop at a leaf test file and forget the domain or top-level re-export path.
- Do not hide startup, auth, or observability regressions behind vague file names.
