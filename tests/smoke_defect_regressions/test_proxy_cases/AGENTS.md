# BACKEND SMOKE PROXY CASES KNOWLEDGE BASE

## OVERVIEW
`test_proxy_cases/` is the focused proxy regression leaf area inside smoke defect coverage. It now warrants its own doc because the folder holds a compact but distinct proxy cluster, separate from the broader proxy aggregator and from the config, costing, and startup smoke trees.

## STRUCTURE
```
test_proxy_cases/
├── healthcheck_and_failover_classification_tests.py
├── logging_model_auth_path_tests.py
├── model_update_proxy_invariant_tests.py
├── proxy_target_runtime_selection_tests.py
├── proxy_unroutable_target_rejection_tests.py
└── recovery_runtime_and_streaming_tests.py
```

## WHERE TO LOOK

- Health-check classification and failover outcome cases: `healthcheck_and_failover_classification_tests.py`
- Logging, model, and auth-path cases: `logging_model_auth_path_tests.py`
- Proxy-model update invariant cases: `model_update_proxy_invariant_tests.py`
- Runtime target selection cases: `proxy_target_runtime_selection_tests.py`
- Unroutable-target rejection and path-guard cases: `proxy_unroutable_target_rejection_tests.py`
- Recovery and streaming cases: `recovery_runtime_and_streaming_tests.py`

## FOLDER NOTES

- This folder is distinct because its six files map to one proxy-focused regression cluster instead of a grab-bag of nearby defect numbers.
- Keep the leaf names explicit so the proxy regression map stays readable when the smoke aggregator expands.
- Keep the parent `test_proxy.py` and top-level smoke aggregator in sync when a new proxy leaf should be re-exported.

## CONVENTIONS

- Keep these leaves focused on proxy routing, model update invariants, logging/auth path behavior, and recovery or streaming semantics.
- Keep the folder separate from the standalone smoke leaves unless a new case truly fits neither place.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Prefer the best current implementation shape over preserving the old one. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS

- Do not move config, startup, costing, or service coverage into this folder.
- Do not hide proxy behavior behind vague file names.
- Do not claim unrelated smoke-case folders also need their own leaf docs unless their density justifies it.
