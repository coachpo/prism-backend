# BACKEND APP KNOWLEDGE BASE

## OVERVIEW
`app/` contains the live backend: FastAPI startup, auth/session enforcement, profile-scope dependencies, routers, service layer, ORM models, and schema contracts.

## STRUCTURE
```
app/
├── main.py                   # Lifespan startup, auth middleware, migrations, seeding, shared httpx client
├── dependencies.py           # DB session + active/effective profile dependencies
├── core/                     # Config, auth/crypto helpers, database, migrations, time helpers
├── models/                   # ORM models and domain splits
├── schemas/                  # Pydantic request/response contracts
├── routers/                  # /api/* management routes, auth/settings, and /v1* proxy entrypoint
└── services/                 # Load balancing, proxying, costing, stats, audit
```

## WHERE TO LOOK

- Startup order and seed logic: `main.py`
- Scope split (`X-Profile-Id` vs active profile): `dependencies.py`
- Cookie auth, refresh, password reset: `routers/auth.py`, `core/auth.py`, `services/auth_service.py`
- Proxy runtime entrypoint: `routers/proxy.py`
- Load-balancing and failover memory: `services/loadbalancer.py`
- Config import/export + blocklist composition: `routers/config.py`, `routers/config_domains/`
- Connection CRUD and provider-specific health checks: `routers/connections.py`, `routers/connections_domains/`
- Auth settings, recovery email verification, proxy API keys: `routers/settings.py`
- Stats re-export boundary: `services/stats_service.py`, `services/stats/`, `services/stats/AGENTS.md`

## CHILD DOCS

- `services/stats/AGENTS.md`: telemetry/spending/query patterns behind `services/stats_service.py`.

## CONVENTIONS

- Keep backend async end-to-end; request handlers, DB access, and upstream HTTP all stay async.
- Use `dependencies.py` to resolve profile scope instead of parsing headers ad hoc inside routers.
- Treat `schemas/schemas.py` and domain schemas as the public contract; frontend types should follow them.
- Use `selectinload` when routers/services need connected models, endpoints, or pricing templates.
- Keep subdomain routers thin when a deeper folder exists (`config_domains`, `connections_domains`, `proxy_domains`).
- Keep auth and proxy-key serialization/building centralized in `services/auth_service.py` and `core/auth.py`.
- Preserve provider-specific health-check behavior in `routers/connections.py`; OpenAI has fallback probes by design.

## APP FACTS

- Lifespan startup sequence is: validate PostgreSQL URL -> run migrations -> seed providers -> enforce profile invariants -> seed user settings -> seed app auth settings -> encrypt endpoint secrets -> seed system header blocklist -> create shared `httpx.AsyncClient`.
- `services/loadbalancer.py` resolves proxy models to native targets and stores failover recovery state by `(profile_id, connection_id)`.
- `main.py` middleware bifurcates auth: `/api/*` uses operator session cookies when enabled, `/v1*` uses proxy API keys when enabled.
- `routers/config.py` is a shell that delegates to `config_domains/import_export.py` and `config_domains/blocklist.py`.
- `services/stats_service.py` is a re-export boundary over `services/stats/`; the real query and logging code lives in that package.

## ANTI-PATTERNS

- Do not use request-scoped DB sessions inside streaming finalization.
- Do not bypass header blocklist sanitization after custom headers are merged.
- Do not duplicate auth gating or proxy-key parsing outside the shared middleware/helpers.
- Do not assume one profile scope model covers both management and proxy traffic.
- Do not reintroduce proxy chaining, unsupported providers, or float money handling.
