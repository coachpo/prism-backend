# BACKEND SCHEMAS KNOWLEDGE BASE

## OVERVIEW
`schemas/` is the backend contract boundary. The domain modules hold the Pydantic models, while `schemas.py` is the explicit re-export surface that route handlers and other callers should import from.

## STRUCTURE
```
schemas/
├── schemas.py             # Explicit public export surface for routers and service callers
└── domains/
    ├── admin.py           # Audit logs, config export or import, blocklist, batch-delete payloads
    ├── auth.py            # Sessions, login, password reset, proxy keys, WebAuthn payloads
    ├── common.py          # Shared schema helpers and enums
    ├── connection_model.py
    ├── core.py            # Profiles, vendors, endpoints, connections, models, pricing templates
    ├── endpoint_pricing.py
    ├── profile_provider.py
    └── stats.py           # Request logs, spending, throughput, metrics batch, loadbalance current-state and event payloads
```

## WHERE TO LOOK

- Supported import surface: `schemas.py`
- Admin contracts for audit logs, config export or import, and blocklist rules: `domains/admin.py`
- Auth and passkey contracts: `domains/auth.py`
- Core management contracts for profiles, vendors, endpoints, connections, models, and pricing templates: `domains/core.py`
- Shared helpers and split support modules behind the public surface: `domains/common.py`, `domains/connection_model.py`, `domains/endpoint_pricing.py`, `domains/profile_provider.py`
- Stats and observability contracts for request logs, spending, throughput, metrics batches, and loadbalance current-state or event payloads: `domains/stats.py`

## SCHEMA FACTS

- `schemas.py` currently re-exports a broad explicit surface from `domains/admin.py`, `domains/auth.py`, `domains/core.py`, and `domains/stats.py`.
- Supporting domain files such as `common.py`, `connection_model.py`, `endpoint_pricing.py`, and `profile_provider.py` still live under `domains/`, but the stable router-facing boundary is `schemas.py`.
- The parent doc covers schema-domain ownership. Don't create new AGENTS docs inside `schemas/domains/` for the current layout.
- Routers should depend on the re-export boundary, not on scattered leaf-module imports.

## CONVENTIONS

- Add or update models in the correct domain file, then re-export them through `schemas.py`.
- Keep field naming aligned with the actual wire contract. Frontend mirror types follow this backend surface.
- Keep response and request shapes explicit in the schema layer instead of constructing anonymous dict contracts in handlers.

## ANTI-PATTERNS

- Do not import domain leaf modules directly from routers when `app.schemas.schemas` already defines the supported surface.
- Do not document internal helper modules as if they are public schema domains when the stable boundary is `admin`, `auth`, `core`, and `stats`.
- Do not let route handlers drift into hand-built payloads that bypass the schema layer.
