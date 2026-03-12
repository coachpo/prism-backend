# BACKEND SMOKE DEFECT REGRESSIONS KNOWLEDGE BASE

## OVERVIEW
`smoke_defect_regressions/` is the regression corpus for named defects and behavior guards. It groups cases by runtime concern, then re-exports them through `tests/test_smoke_defect_regressions.py`.

## STRUCTURE
```
smoke_defect_regressions/
├── test_proxy_cases/         # Routing, failover, streaming, model/path handling
├── test_config_cases/        # Import/export, validation, user-setting seeding
├── test_costing_cases/       # Token parsing, missing-price handling, pricing template CAS
├── test_startup_cases/       # Auth, CORS, batch delete, model health, owner mapping
├── test_proxy.py             # Proxy-domain aggregator
├── test_config.py            # Config-domain aggregator
├── test_costing.py           # Costing-domain aggregator
├── test_startup.py           # Startup-domain aggregator
├── test_failover.py          # Standalone recovery toggle regression
├── test_headers.py           # Header behavior regression
├── test_endpoint_ordering.py # Endpoint ordering regression
└── test_connection_priority.py
```

## WHERE TO LOOK

- High-level export surface: `../test_smoke_defect_regressions.py`
- Auth, password reset, email delivery, proxy-key acceptance, secret sanitization: `test_startup_cases/auth_management_flows_tests.py`
- CORS auth middleware guard: `test_startup_cases/cors_preflight_auth_middleware_tests.py`
- Request-log batch delete, timezone normalization, endpoint-owner mapping: `test_startup_cases/stats_timezone_batch_delete_and_endpoint_mapping_tests.py`
- Profile scope and model-health eager loading: `test_startup_cases/profile_scope_and_model_health_eagerload_tests.py`
- Recovery, streaming, runtime failover: `test_proxy_cases/recovery_runtime_and_streaming_tests.py`
- Health-check classification and provider-path validation: `test_proxy_cases/healthcheck_and_failover_classification_tests.py`
- Config v2 roundtrip and validation: `test_config_cases/`
- Special-token costing and pricing template CAS: `test_costing_cases/`

## CONVENTIONS

- New regression classes use `TestDEF###_*` naming; keep IDs unique and append-only.
- Domain-specific files can be large; preserve semantic grouping by concern instead of splitting into arbitrary unit or integration buckets.
- Aggregator files are suite shape, not convenience wrappers; when you add a case, update the relevant aggregator and top-level export if needed.
- Keep startup-domain regressions explicit in file and class names; auth, CORS, batch delete, and model-health concerns should stay visible.

## ANTI-PATTERNS

- Do not drop a new DEF case into the wrong concern folder just because it is nearby.
- Do not add a regression only in a leaf file and forget the aggregator re-export path.
- Do not hide profile-scope or auth regressions in generic startup names; make the concern obvious in file or class naming.
