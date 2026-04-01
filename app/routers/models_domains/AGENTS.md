# BACKEND MODELS DOMAINS KNOWLEDGE BASE

## OVERVIEW
`models_domains/` is the model-management package behind `../models.py`. It splits read paths from mutation paths, covering model list/detail queries, endpoint-batch lookups, and create/update/delete flows for native and proxy models.

## STRUCTURE
```
models_domains/
├── handlers.py           # Stable export surface used by `models.py`
├── query_handlers.py     # List/detail and by-endpoint reads
├── query_helpers.py      # Query-specific shaping and shared loaders
├── mutation_handlers.py  # Create/update/delete flows
└── mutation_helpers.py   # Mutation validation and payload helpers
```

## WHERE TO LOOK

- Shell-router import surface: `handlers.py`
- List/detail and by-endpoint read flows: `query_handlers.py`, `query_helpers.py`
- Create/update/delete flows and proxy/native mutation rules: `mutation_handlers.py`, `mutation_helpers.py`

## CONVENTIONS

- Keep `models.py` thin and import through `handlers.py`.
- Keep list/detail and by-endpoint reads in `query_*` modules, including health-stat hydration and eager loading.
- Keep create/update/delete logic and proxy/native validation in `mutation_*` modules.
- Reuse shared loaders and shaping helpers instead of duplicating model-detail fetch logic in both read and write paths.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Prefer the best current implementation shape over preserving the old one. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS

- Do not mix eager-load query shaping with mutation validation.
- Do not duplicate model health-stat enrichment outside the query layer.
- Do not push proxy-target or mutation invariants back into `models.py`.
