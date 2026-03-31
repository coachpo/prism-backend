# BACKEND STARTUP SMOKE REGRESSION CLUSTER KNOWLEDGE BASE

## OVERVIEW
`test_startup_cases/` is the dense startup-side smoke regression cluster behind `../test_startup.py`. It groups startup, auth, middleware, health-contract, schema, storage-mode, and vendor-safety regressions that still participate in the top-level DEF smoke export surface.

## STRUCTURE
```
test_startup_cases/
├── auth_management_flows_tests.py                    # Operator auth enablement, login, logout, password reset, email delivery failures
├── auth_proxy_key_generation_tests.py                # Proxy API key generation and metadata-management regressions
├── cors_preflight_auth_middleware_tests.py           # CORS preflight bypass behavior for auth middleware
├── health_contract_tests.py                          # `/health` status and version contract
├── loadbalance_delete_contract_tests.py              # Loadbalance event batch-delete contract
├── loadbalance_profile_primary_key_tests.py          # Profile/vendor primary-key contract regression
├── loadbalance_strategy_preset_seed_tests.py         # Default adaptive routing preset seeding at startup
├── logging_endpoint_owner_and_connection_defaults_tests.py # Logging endpoint ownership and connection-default regressions
├── observability_storage_mode_tests.py               # Observability persistence-mode regression
├── profile_scope_and_model_health_eagerload_tests.py # Profile-scope and model-health eager-load regressions
├── proxy_target_schema_tests.py                      # Proxy-target schema regression
├── stats_timezone_batch_delete_and_endpoint_mapping_tests.py # Stats timezone normalization, batch delete, endpoint mapping regressions
├── usage_snapshot_storage_tests.py                   # Usage-snapshot route/storage regression
├── vendor_api_family_schema_tests.py                 # Vendor/api-family schema regression
└── vendor_delete_safety_tests.py                     # Vendor delete safety and dependency contract regression
```

## WHERE TO LOOK

- Startup-domain re-export surface: `../test_startup.py`
- Top-level DEF export surface: `../../test_smoke_defect_regressions.py`
- Auth lifecycle and recovery-email regressions: `auth_management_flows_tests.py`
- Proxy-key issue and metadata-management regressions: `auth_proxy_key_generation_tests.py`
- Middleware and health contracts: `cors_preflight_auth_middleware_tests.py`, `health_contract_tests.py`
- Startup schema and seed regressions: `loadbalance_profile_primary_key_tests.py`, `loadbalance_strategy_preset_seed_tests.py`, `observability_storage_mode_tests.py`, `proxy_target_schema_tests.py`
- Stats, endpoint mapping, and usage-snapshot regressions: `stats_timezone_batch_delete_and_endpoint_mapping_tests.py`, `usage_snapshot_storage_tests.py`
- Vendor/schema safety regressions: `vendor_api_family_schema_tests.py`, `vendor_delete_safety_tests.py`

## CONVENTIONS

- Put startup-side named defects here first, then re-export them through `../test_startup.py` and `../../test_smoke_defect_regressions.py` when they belong in the top-level DEF corpus.
- Keep file names explicit about the guarded startup contract so regressions stay searchable by concern.
- Keep this cluster focused on startup, auth, middleware, schema, seed, storage, and closely related contract regressions.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS

- Do not strand a new startup-side DEF leaf here without wiring the aggregator path above it.
- Do not move service-focused or general proxy runtime tests here just because startup touched the same codepath.
- Do not collapse multiple startup contracts into vague filenames that hide the guarded behavior.
