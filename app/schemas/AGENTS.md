# BACKEND SCHEMAS KNOWLEDGE BASE

## OVERVIEW
`schemas/` is the backend contract layer: domain-scoped Pydantic models plus the `schemas.py` re-export surface imported by routers.

## STRUCTURE
```
schemas/
├── schemas.py                 # Public re-export boundary for router imports
└── domains/
    ├── admin.py              # Audit/config/blocklist payloads
    ├── auth.py               # Session, password reset, proxy-key, WebAuthn payloads
    ├── common.py             # Shared base helpers and enums
    ├── connection_model.py   # Connection and model payloads
    ├── core.py               # Endpoint, pricing-template, provider, profile payloads
    ├── endpoint_pricing.py   # Pricing-specific endpoint payload helpers
    ├── profile_provider.py   # Profile/provider shared response shapes
    └── stats.py              # Request-log, spending, throughput, realtime payloads
```

## WHERE TO LOOK

- Router import surface: `schemas.py`
- Auth and passkey contracts: `domains/auth.py`
- Profile, endpoint, connection, model, and pricing contracts: `domains/core.py`, `domains/connection_model.py`, `domains/endpoint_pricing.py`, `domains/profile_provider.py`
- Audit/config/admin contracts: `domains/admin.py`
- Stats, throughput, loadbalance, and realtime update payloads: `domains/stats.py`

## CONVENTIONS

- Add new Pydantic models to the matching domain module, then re-export them through `schemas.py`.
- Keep field names aligned with the JSON contract; frontend types intentionally mirror snake_case responses.
- Treat this layer as the public contract boundary for routers and the frontend type mirror.

## ANTI-PATTERNS

- Do not import leaf domain schemas ad hoc from routers when `app.schemas.schemas` already re-exports the supported surface.
- Do not camelCase API payload fields here; contract naming should mirror wire format.
- Do not let route handlers invent response shapes that bypass the schema layer.
