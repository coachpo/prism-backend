# BACKEND SMOKE DEFECT REGRESSIONS KNOWLEDGE BASE

## OVERVIEW
`smoke_defect_regressions/` is the named-defect regression corpus. It groups DEF cases by concern and re-exports them through `../test_smoke_defect_regressions.py`.

## STRUCTURE
```text
smoke_defect_regressions/
├── AGENTS.md
├── test_config_cases/
│   └── AGENTS.md
├── test_costing_cases/
│   └── AGENTS.md
├── test_proxy_cases/
│   └── AGENTS.md
├── test_startup_cases/
│   └── AGENTS.md
├── test_config.py
├── test_costing.py
├── test_proxy.py
├── test_startup.py
├── test_failover.py
├── test_headers.py
├── test_connection_priority.py
├── test_conditional_decompression.py
└── test_endpoint_ordering.py
```

## WHERE TO LOOK
- `../test_smoke_defect_regressions.py`: top-level DEF export surface
- `test_config_cases/`: config export/import, schema validation, and user-settings seed coverage.
- `test_costing_cases/`: token parsing, special-token rules, and pricing-template CAS coverage.
- `test_proxy_cases/`: routing, failover, streaming, and model or path guards.
- `test_startup_cases/`: auth, CORS, batch delete, model health, schema, and startup contracts.
- Standalone guards outside the grouped folders: `test_failover.py`, `test_headers.py`, `test_connection_priority.py`, `test_conditional_decompression.py`, `test_endpoint_ordering.py`

## DEF FACTS
- The current top-level smoke export surface reaches DEF088, while DEF087 still appears in both startup and proxy leaves.
- `test_config_cases/` and `test_costing_cases/` now join the existing proxy and startup leaves as justified child docs because each folder holds a focused multi-file regression cluster.

## BOUNDARY NOTES
- Keep PostgreSQL grounding explicit here, because these DEF regressions exercise the same testcontainer-backed database semantics as the rest of `tests/`.
- Keep smoke coverage focused on named defects and the re-export surface. Move auth cache, background task, crypto, loadbalancer, stats, streaming, throughput, and WebAuthn service cases to `../services/AGENTS.md` instead of growing this tree.

## FOLDER NOTES
- `test_config_cases/` and `test_costing_cases/` justify their own leaf docs because each groups a focused regression cluster with multiple files.
- `test_proxy_cases/` is a topic-specific proxy lookup cluster spanning health classification, logging/auth paths, model-update invariants, runtime target selection, and recovery or streaming behavior.
- `test_startup_cases/` is the densest startup-side cluster, spanning auth flows, middleware, health-contract checks, schema guards, usage-snapshot storage checks, and vendor/schema regressions.

## CONVENTIONS
- Keep DEF numbering explicit and aligned with the aggregator's current export surface.
- Put new regressions in the right concern folder first.
- Keep file names explicit about the guarded behavior.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS
- Do not reuse DEF numbers or insert a new regression into the wrong concern folder just because it is nearby.
- Do not stop at a leaf test file and forget the domain or top-level re-export path.
- Do not hide startup, auth, or observability regressions behind vague file names.
