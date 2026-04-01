# BACKEND CONNECTIONS DOMAINS KNOWLEDGE BASE

## OVERVIEW
`connections_domains/` owns the dense connection-management helpers behind `../connections.py`. It covers route assembly, CRUD orchestration, owner lookups, health-check plumbing, and the nested CRUD-handler cluster that keeps the shell router thin.

## STRUCTURE
```
connections_domains/
├── route_handlers.py               # Route assembly and shared handler wiring
├── crud_route_handlers.py          # API-facing CRUD endpoints
├── crud_handlers/                  # Listing, creation, updating, pricing, reordering, deletion, shared helpers
├── owner_route_handlers.py         # Owner-scoped helpers and filtering
├── health_route_handlers.py        # Health-check endpoints and responses
├── connection_crud_helpers.py      # Shared CRUD validation and mutation helpers
├── crud_dependencies.py            # Shared route dependencies and lookup helpers
├── health_check_builders.py        # Health-check request and payload builders
└── health_check_request_helpers.py # Health-check transport helpers
```

## WHERE TO LOOK

- Route assembly and handler wiring: `route_handlers.py`
- API-facing CRUD routes: `crud_route_handlers.py`
- Nested CRUD operations: `crud_handlers/`, especially `listing.py`, `creation.py`, `updating.py`, `pricing.py`, `reordering.py`, and `deletion.py`
- Shared CRUD validation and lookup helpers: `connection_crud_helpers.py`, `crud_dependencies.py`
- Owner-scoped routing helpers: `owner_route_handlers.py`
- Health-check endpoints and request building: `health_route_handlers.py`, `health_check_builders.py`, `health_check_request_helpers.py`

## CONVENTIONS

- Keep `connections.py` thin. Put connection-specific request logic in this package instead of the shell router.
- Keep the API-facing CRUD layer in `crud_route_handlers.py`, then push low-level mutations and ordering/pricing helpers into `crud_handlers/`.
- Keep health-check request building separate from route responses so it can be reused by multiple handlers.
- Keep owner-scoped filtering explicit instead of folding it into generic CRUD helpers.
- Keep the nested `crud_handlers/` cluster documented here; it supports this package and does not need another AGENTS file.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Prefer the best current implementation shape over preserving the old one. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS

- Do not move connection-specific logic back into `connections.py`.
- Do not blur CRUD helpers with health-check helpers when the package already splits those responsibilities.
- Do not duplicate router-wide profile or auth rules here; those live in the parent router map and app dependencies.
- Do not split `crud_handlers/` into a second leaf doc while this parent already owns that cluster.
