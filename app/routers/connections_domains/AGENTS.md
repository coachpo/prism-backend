# BACKEND CONNECTIONS DOMAINS KNOWLEDGE BASE

## OVERVIEW
`connections_domains/` owns the dense connection-management helpers behind `../connections.py`. It covers CRUD wiring, owner lookups, health-check plumbing, and the request helpers that keep the shell router thin.

## STRUCTURE
```
connections_domains/
├── route_handlers.py               # Route assembly and shared handler wiring
├── crud_route_handlers.py          # Connection CRUD endpoints
├── owner_route_handlers.py         # Owner-scoped helpers and filtering
├── health_route_handlers.py        # Health-check endpoints and responses
├── connection_crud_helpers.py      # CRUD validation and mutation helpers
├── crud_dependencies.py            # Shared route dependencies and lookup helpers
├── health_check_builders.py        # Health-check request and payload builders
├── health_check_request_helpers.py # Health-check request helpers
└── __init__.py
```

## WHERE TO LOOK

- Route assembly and handler wiring: `route_handlers.py`
- Connection CRUD flows and payload shaping: `crud_route_handlers.py`, `connection_crud_helpers.py`
- Owner-scoped routing helpers: `owner_route_handlers.py`
- Health-check endpoints and request building: `health_route_handlers.py`, `health_check_builders.py`, `health_check_request_helpers.py`
- Shared dependencies and lookup helpers: `crud_dependencies.py`

## CONVENTIONS

- Keep `connections.py` thin. Put connection-specific request logic in this package instead of the shell router.
- Keep CRUD validation and mutation helpers local to this package so the route handlers stay small.
- Keep health-check request building separate from route responses so it can be reused by multiple handlers.
- Keep owner-scoped filtering explicit instead of folding it into generic CRUD helpers.

## ANTI-PATTERNS

- Do not move connection-specific logic back into `connections.py`.
- Do not blur CRUD helpers with health-check helpers when the package already splits those responsibilities.
- Do not duplicate router-wide profile or auth rules here, those live in the parent router map and app dependencies.
