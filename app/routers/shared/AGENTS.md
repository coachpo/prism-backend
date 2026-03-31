# BACKEND ROUTERS SHARED HELPERS KNOWLEDGE BASE

## OVERVIEW
`routers/shared/` holds small reusable helpers used by multiple management-route packages. Keep router-layer ordering, endpoint-record validation, and profile-row locking here instead of duplicating those seams across domain packages.

## STRUCTURE
```
shared/
├── endpoint_records.py  # Endpoint-name uniqueness and next-position helpers
├── ordering.py          # Generic ordered-field normalization helper
└── profile_rows.py      # Row-lock helper for profile-scoped mutations
```

## WHERE TO LOOK

- Endpoint uniqueness checks and next-position allocation: `endpoint_records.py`
- Ordered-field normalization for mutable lists: `ordering.py`
- Profile row locking before profile-scoped mutation flows: `profile_rows.py`
- Router shells and domain packages that consume these helpers: `../AGENTS.md`, `../connections_domains/AGENTS.md`, `../endpoints_domains/AGENTS.md`, `../models_domains/AGENTS.md`

## CONVENTIONS

- Keep helpers here small, reusable, and router-layer focused.
- Raise router-facing exceptions here only when the helper directly owns that validation boundary, as `endpoint_records.py` does for duplicate endpoint names.
- Keep row-locking and ordered-field normalization generic enough for reuse across management routes.
- Let service packages keep business logic and side effects; `shared/` is for cross-router helper seams, not service orchestration.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS

- Do not move full route handlers or domain-specific mutation workflows into `shared/`.
- Do not duplicate endpoint-position, endpoint-name, or profile-row lock helpers inside multiple router packages.
- Do not put auth parsing, profile-header resolution, or runtime proxy logic here. Those boundaries live higher in app dependencies and router-domain packages.
