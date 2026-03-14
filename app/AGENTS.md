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
├── routers/                  # /api/* management routes, auth/settings, pricing templates, /v1* proxy entrypoint
└── services/                 # Load balancing, proxying, auth, costing, stats, audit
```

## WHERE TO LOOK

- Startup order and seed logic: `main.py`
- Scope split (`X-Profile-Id` vs active profile): `dependencies.py`
- Cookie auth, refresh rotation, verified email, password reset, proxy API keys: `routers/auth.py`, `routers/settings.py`, `services/auth_service.py`, `core/auth.py`
- Proxy runtime entrypoint: `routers/proxy.py`, `routers/proxy_domains/`
- Load-balancing and failover memory: `services/loadbalancer.py`
- Config import/export + blocklist composition: `routers/config.py`, `routers/config_domains/`
- Connection CRUD and provider-specific health checks: `routers/connections.py`, `routers/connections_domains/`
- Pricing templates and connection pricing linkage: `routers/pricing_templates.py`
- Stats re-export boundary: `services/stats_service.py`, `services/stats/`, `services/stats/AGENTS.md`

## CHILD DOCS

- `services/stats/AGENTS.md`: telemetry, request-log, and spending query patterns behind `services/stats_service.py`.

## CONVENTIONS

- Keep backend async end-to-end; request handlers, DB access, and upstream HTTP all stay async.
- Use `dependencies.py` to resolve profile scope instead of parsing headers ad hoc inside routers.
- Treat domain schemas as the public contract; frontend types should follow them.
- Use `selectinload` when routers or services need connected models, endpoints, or pricing templates.
- Keep subdomain routers thin when a deeper folder exists (`config_domains`, `connections_domains`, `proxy_domains`).
- Keep auth and proxy-key serialization/building centralized in `services/auth_service.py` and `core/auth.py`.
- Preserve provider-specific health-check behavior in `routers/connections.py`; OpenAI has fallback probes by design.

## APP FACTS

- Lifespan startup sequence is: validate PostgreSQL URL -> run migrations -> seed providers -> enforce profile invariants -> seed user settings -> seed app auth settings -> encrypt endpoint secrets -> seed system header blocklist -> create shared `httpx.AsyncClient`.
- `main.py` middleware bifurcates auth: `/api/*` uses operator session cookies when enabled, `/v1*` uses proxy API keys when enabled.
- `services/auth_service.py` is the public re-export boundary for verified-email gating, refresh-token family rotation and revocation, password-reset OTP flows, SMTP delivery, proxy-key CRUD, and proxy-key serialization implemented under `services/auth/`.
- `routers/config.py` is a shell that delegates to `config_domains/import_export.py` and `config_domains/blocklist.py`.
- `services/stats_service.py` is a re-export boundary over `services/stats/`; the real query and logging code lives in that package.

## ANTI-PATTERNS

- Do not use request-scoped DB sessions inside streaming finalization.
- Do not bypass header blocklist sanitization after custom headers are merged.
- Do not duplicate auth gating, refresh-token, or proxy-key parsing outside the shared middleware and helpers.
- Do not assume one profile scope model covers both management and proxy traffic.
- Do not reintroduce proxy chaining, unsupported providers, or float money handling.
