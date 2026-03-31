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
    ├── monitoring.py      # Monitoring overview, vendor, model, and manual-probe payloads
    ├── endpoint_pricing.py
    ├── profile_vendor.py
    ├── stats.py           # Request logs, spending, throughput, metrics batch, loadbalance current-state and event payloads
    └── usage_statistics.py # Unified usage-snapshot and request-event payloads
```

## WHERE TO LOOK

- Supported import surface: `schemas.py`
- Admin contracts for audit logs, config export or import, and blocklist rules: `domains/admin.py`
- Auth and passkey contracts: `domains/auth.py`
- Core management contracts for profiles, vendors, endpoints, connections, models, and pricing templates: `domains/core.py`
- Monitoring overview, drill-down, and manual-probe contracts: `domains/monitoring.py`
- Shared helpers and split support modules behind the public surface: `domains/common.py`, `domains/connection_model.py`, `domains/endpoint_pricing.py`, `domains/profile_vendor.py`
- Stats and observability contracts for request logs, spending, throughput, metrics batches, and loadbalance current-state or event payloads: `domains/stats.py`
- Unified usage-snapshot and request-event contracts: `domains/usage_statistics.py`

## SCHEMA FACTS

- `schemas.py` currently re-exports a broad explicit surface from `domains/admin.py`, `domains/auth.py`, `domains/core.py`, `domains/monitoring.py`, `domains/stats.py`, and `domains/usage_statistics.py`.
- Config export/import payloads use the current top-level `version: 1` format, and the admin schemas carry the vendor `icon_key` field on vendor payloads only.
- Loadbalance strategy management contracts now use one `routing_policy` document with `hedge`, `circuit_breaker`, `admission`, and `monitoring` branches.
- Supporting domain files such as `common.py`, `connection_model.py`, `endpoint_pricing.py`, and `profile_vendor.py` still live under `domains/`, but the stable router-facing boundary is `schemas.py`.
- The parent doc covers schema-domain ownership. Don't create new AGENTS docs inside `schemas/domains/` for the current layout.
- Routers should depend on the re-export boundary, not on scattered leaf-module imports.

## CONVENTIONS

- Add or update models in the correct domain file, then re-export them through `schemas.py`.
- Keep field naming aligned with the actual wire contract. Frontend mirror types follow this backend surface.
- Keep response and request shapes explicit in the schema layer instead of constructing anonymous dict contracts in handlers.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS

- Do not import domain leaf modules directly from routers when `app.schemas.schemas` already defines the supported surface.
- Do not document internal helper modules as if they are public schema domains when the stable boundary is `admin`, `auth`, `core`, `monitoring`, and `stats`.
- Do not let route handlers drift into hand-built payloads that bypass the schema layer.
