# BACKEND ENDPOINTS DOMAINS KNOWLEDGE BASE

## OVERVIEW
`endpoints_domains/` is the endpoint-management package behind `../endpoints.py`. It owns list, create, update, duplicate, delete, reorder, and connection-dropdown responses for profile-scoped endpoints.

## STRUCTURE
```
endpoints_domains/
├── route_handlers.py   # Endpoint CRUD, duplication, reorder, and dropdown responses
└── helpers.py          # Naming, ordering, usage, and row-lock helpers
```

## WHERE TO LOOK

- List/create/update/delete/duplicate/reorder responses: `route_handlers.py`
- Endpoint-name uniqueness, ordering, dependent usage checks, and row locking: `helpers.py`

## CONVENTIONS

- Keep `endpoints.py` thin and let this package own endpoint-specific request logic.
- Normalize and validate base URLs through the shared proxy-service helpers before persisting them.
- Keep ordering, duplicate-name generation, dependency checks, and profile-row locking in `helpers.py`.
- Clear dependent recovery state when base URL or API key changes instead of leaving stale load-balance state behind.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS

- Do not rebuild base-URL validation or normalization outside `route_handlers.py` and `helpers.py`.
- Do not mutate endpoint ordering in the shell router.
- Do not delete an in-use endpoint without the explicit usage-row guard.
