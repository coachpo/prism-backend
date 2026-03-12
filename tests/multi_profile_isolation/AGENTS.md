# BACKEND MULTI-PROFILE ISOLATION KNOWLEDGE BASE

## OVERVIEW
`multi_profile_isolation/` verifies the selected-vs-active profile model end to end: lifecycle limits, management scoping, runtime routing isolation, observability attribution, and config import/export containment.

## STRUCTURE
```
multi_profile_isolation/
├── test_lifecycle.py
├── test_scoping.py
├── test_runtime.py
├── test_observability.py
├── test_config_import_export.py
├── test_config_import_export_cases/   # Profile-scoped import/export leaf cases
└── test_connection_priority_isolation.py
```

## WHERE TO LOOK

- Lifecycle and CRUD guards: `test_lifecycle.py`
- Effective-profile management scoping: `test_scoping.py`
- Runtime model resolution and failover-state isolation: `test_runtime.py`
- Costing, settings, audit, and blocklist attribution: `test_observability.py`
- Profile-targeted config import/export: `test_config_import_export.py`, `test_config_import_export_cases/profile_scoped_config_export_import_isolation_tests.py`
- Priority and ordering isolation: `test_connection_priority_isolation.py`
- Top-level suite export: `../test_multi_profile_isolation.py`

## CONVENTIONS

- Keep tests framed in FR and profile-isolation semantics; many files map directly to requirement IDs rather than CRUD surfaces.
- Runtime tests should prove that profile A and profile B can share overlapping IDs without cross-talk.
- Observability tests should assert immutable `profile_id` attribution, not just response payload shape.
- Config-import cases belong here when they prove isolation boundaries, even if similar validation exists in defect regressions.

## ANTI-PATTERNS

- Do not write isolation tests that rely on a single-profile fixture shape.
- Do not assert management-scope behavior using runtime-only dependencies.
- Do not collapse selected-profile and active-profile expectations into one assertion.
