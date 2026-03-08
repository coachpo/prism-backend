# BACKEND APP KNOWLEDGE BASE

## OVERVIEW
`app/` contains the live backend: FastAPI startup, profile-scope dependencies, routers, service layer, ORM models, and schema contracts.

## STRUCTURE
```
app/
├── main.py                   # Lifespan startup, migrations, seeding, shared httpx client
├── dependencies.py           # DB session + active/effective profile dependencies
├── core/                     # Config, database, migrations, time helpers
├── models/                   # ORM models and domain splits
├── schemas/                  # Pydantic request/response contracts
├── routers/                  # /api/* management routes + /v1* proxy entrypoint
└── services/                 # Load balancing, proxying, costing, stats, audit
```

## WHERE TO LOOK

- Startup order and seed logic: `main.py`
- Scope split (`X-Profile-Id` vs active profile): `dependencies.py`
- Proxy runtime entrypoint: `routers/proxy.py`
- Load-balancing and failover memory: `services/loadbalancer.py`
- Config import/export + blocklist composition: `routers/config.py`, `routers/config_domains/`
- Connection CRUD and provider-specific health checks: `routers/connections.py`, `routers/connections_domains/`
- Stats re-export boundary: `services/stats_service.py`, `services/stats/`

## CONVENTIONS

- Keep backend async end-to-end; request handlers, DB access, and upstream HTTP all stay async.
- Use `dependencies.py` to resolve profile scope instead of parsing headers ad hoc inside routers.
- Treat `schemas/schemas.py` and domain schemas as the public contract; frontend types should follow them.
- Use `selectinload` when routers/services need connected models, endpoints, or pricing templates.
- Keep subdomain routers thin when a deeper folder exists (`config_domains`, `connections_domains`, `proxy_domains`).
- Preserve provider-specific health-check behavior in `routers/connections.py`; OpenAI has fallback probes by design.

## APP FACTS

- Lifespan startup sequence is: validate PostgreSQL URL -> run migrations -> seed providers -> enforce profile invariants -> seed user settings -> seed system header blocklist -> create shared `httpx.AsyncClient`.
- `services/loadbalancer.py` resolves proxy models to native targets and stores failover recovery state by `(profile_id, connection_id)`.
- `routers/config.py` is a shell that delegates to `config_domains/import_export.py` and `config_domains/blocklist.py`.
- `services/stats_service.py` is a re-export boundary over `services/stats/`; the real query and logging code lives in that package.

## ANTI-PATTERNS

- Do not use request-scoped DB sessions inside streaming finalization.
- Do not bypass header blocklist sanitization after custom headers are merged.
- Do not assume one profile scope model covers both management and proxy traffic.
- Do not reintroduce proxy chaining, unsupported providers, or float money handling.
