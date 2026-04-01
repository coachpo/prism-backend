# BACKEND MULTI-PROFILE ISOLATION KNOWLEDGE BASE

## OVERVIEW
`multi_profile_isolation/` verifies the selected-profile versus active-profile split across management and runtime behavior. The suite covers lifecycle, management scoping, runtime routing, observability attribution, connection-priority isolation, and profile-scoped config export or import isolation.

## STRUCTURE
```
multi_profile_isolation/
├── test_lifecycle.py
├── test_scoping.py
├── test_runtime.py
├── test_observability.py
├── test_config_import_export.py
├── test_config_import_export_cases/   # Profile-scoped config export or import isolation leaf cases
└── test_connection_priority_isolation.py
```

## WHERE TO LOOK

- Top-level isolation export surface for the aggregator-backed subset: `../test_multi_profile_isolation.py`
- Lifecycle and CRUD limits: `test_lifecycle.py`
- Effective-profile management scoping and leakage prevention: `test_scoping.py`
- Runtime model resolution and failover recovery-state isolation: `test_runtime.py`
- Costing, settings, header blocklist, and observability attribution: `test_observability.py`
- Config export or import containment: `test_config_import_export.py`, `test_config_import_export_cases/profile_scoped_config_export_import_isolation_tests.py`
- Connection priority isolation across profiles: `test_connection_priority_isolation.py`

## ISOLATION FACTS

- `../test_multi_profile_isolation.py` aggregates lifecycle, scoping, runtime, observability, and config export or import isolation for this subtree.
- `test_connection_priority_isolation.py` currently lives here as a direct subtree leaf and is not re-exported by `../test_multi_profile_isolation.py`.
- The suite explicitly includes profile-scoped config export or import isolation, not just general config validation.
- Parent coverage for `test_config_import_export_cases/` lives here. Don't add another AGENTS doc under that folder for the current structure.

## BOUNDARY NOTES

- Keep PostgreSQL grounding explicit here, because the isolation cases depend on the same testcontainer-backed database behavior as the rest of `tests/`.
- Keep this tree for cross-profile containment, not service-level concerns or broad backend behavior. Focused auth cache, background task, crypto, loadbalancer, stats, streaming, throughput, and WebAuthn tests belong in `../services/AGENTS.md`.

## CONVENTIONS

- Keep tests framed around cross-profile containment, not single-profile CRUD behavior.
- Assert both sides of the split when relevant: selected profile for management scope, active profile for runtime routing.
- Keep observability assertions tied to `profile_id` attribution and profile-scoped settings or blocklist behavior, not just response status.
- Put connection-priority or config import or export tests here when the point is isolation between profiles.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Prefer the best current implementation shape over preserving the old one. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS

- Do not write isolation tests that only prove one profile works in isolation.
- Do not collapse selected-profile and active-profile expectations into one generic assertion.
- Do not move profile-scoped config export or import isolation cases into the DEF tree when the main point is cross-profile containment.
