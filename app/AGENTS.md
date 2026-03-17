# BACKEND APP KNOWLEDGE BASE

## OVERVIEW
`app/` contains the live backend: FastAPI startup, auth/session enforcement, profile-scope dependencies, routers, service-layer entrypoints, a lifespan-managed background task worker, ORM models, schema contracts, realtime transport, pricing logic, and loadbalance/audit plumbing.

## STRUCTURE
```
app/
├── main.py                                   # Lifespan startup, auth middleware, shared httpx client, background task worker
├── bootstrap/AGENTS.md                       # Startup sequencing and auth middleware split
├── dependencies.py                           # DB session + active/effective profile dependencies
├── core/AGENTS.md                            # Config, auth/crypto helpers, database, migrations, time helpers
├── models/AGENTS.md                          # ORM models and domain splits
├── schemas/AGENTS.md                         # Pydantic request/response contracts
├── routers/AGENTS.md                         # 14 router shells + *_domains/ API layout
├── services/AGENTS.md                        # Service-root boundaries, facades, cleanup helpers, worker
├── services/auth/AGENTS.md                   # Session/email/password-reset/proxy-key internals
├── services/loadbalancer_support/AGENTS.md   # Recovery state, attempts, event helpers
├── services/proxy_support/AGENTS.md          # Upstream URL/header/body/transport helpers
├── services/realtime/AGENTS.md               # WebSocket room management and broadcasts
├── services/stats/AGENTS.md                  # Telemetry, request-log, and spending queries
└── services/webauthn/AGENTS.md               # Passkey registration, authentication, and credentials
```

## WHERE TO LOOK

- Startup order and seed logic: `main.py`, `bootstrap/startup.py`
- Scope split (`X-Profile-Id` vs active profile): `dependencies.py`
- Cookie auth, refresh rotation, verified email, password reset, proxy API keys, passkeys: `routers/auth.py`, `routers/settings.py`, `services/auth_service.py`, `services/webauthn_service.py`, `services/webauthn/`, `core/auth.py`
- Proxy runtime entrypoint: `routers/proxy.py`, `routers/proxy_domains/`
- Load-balancing, recovery state, and event emission: `services/loadbalancer.py`, `services/loadbalancer_support/`, `routers/loadbalance.py`
- Config import/export + blocklist composition: `routers/config.py`, `routers/config_domains/`
- Connection CRUD and provider-specific health checks: `routers/connections.py`, `routers/connections_domains/`
- Pricing templates and connection pricing linkage: `routers/pricing_templates.py`, `services/costing_service.py`
- Audit and realtime payload emission: `routers/audit.py`, `routers/realtime.py`, `services/audit_service.py`, `services/realtime/connection_manager.py`
- Stats re-export boundary: `services/stats_service.py`, `services/stats/`, `services/stats/AGENTS.md`
- Service-root utilities and worker: `services/AGENTS.md`, `services/background_tasks.py`, `services/profile_invariants.py`, `services/user_settings.py`, `services/background_cleanup.py`, `services/loadbalance_cleanup.py`

## CHILD DOCS

- `bootstrap/AGENTS.md`: startup sequence, seed defaults, auth bifurcation, and CORS-aware auth error responses.
- `core/AGENTS.md`: settings, engine/session factories, auth helpers, crypto, migrations, and shared time utilities.
- `models/AGENTS.md`: ORM models split into identity, routing, and observability domains.
- `routers/AGENTS.md`: top-level API shells, domain folders, and dependency split between management, proxy, and realtime routers.
- `schemas/AGENTS.md`: contract-layer ownership for domain Pydantic models and the `schemas.py` re-export boundary.
- `services/AGENTS.md`: service-root public facades, cleanup helpers, and lifespan-managed background task infrastructure.
- `services/auth/AGENTS.md`: auth/session/email/password-reset/proxy-key internals behind `services/auth_service.py`.
- `services/loadbalancer_support/AGENTS.md`: recovery-state mutation, attempt planning, and loadbalance-event helpers behind `services/loadbalancer.py`.
- `services/proxy_support/AGENTS.md`: upstream URL/header/body/compression/transport helpers behind `services/proxy_service.py`.
- `services/realtime/AGENTS.md`: connection-manager room state and broadcast helpers behind `routers/realtime.py`.
- `services/stats/AGENTS.md`: telemetry, request-log, and spending query patterns behind `services/stats_service.py`.
- `services/webauthn/AGENTS.md`: passkey registration, authentication, and credential management behind `services/webauthn_service.py`.

## CONVENTIONS

- Keep backend async end-to-end; request handlers, DB access, and upstream HTTP all stay async.
- Use `dependencies.py` to resolve profile scope instead of parsing headers ad hoc inside routers.
- Treat domain schemas as the public contract; frontend types should follow them.
- Use `selectinload` when routers or services need connected models, endpoints, or pricing templates.
- Keep subdomain routers thin when a deeper folder exists (`auth_domains`, `config_domains`, `connections_domains`, `models_domains`, `proxy_domains`, `settings_domains`).
- Keep lifespan-owned infrastructure (`httpx.AsyncClient`, `background_task_manager`) initialized in `main.py`; leaf services should treat them as app-owned dependencies.
- Keep auth and proxy-key serialization/building centralized in `services/auth_service.py` and `core/auth.py`.
- Keep passkey registration/authentication/credential management in `services/webauthn_service.py` and `services/webauthn/` instead of treating it as part of `services/auth/`.
- Keep proxy URL/header/body/compression logic in `services/proxy_support/`; keep recovery-state mutation and attempt planning in `services/loadbalancer_support/`.
- Keep websocket room management in `services/realtime/connection_manager.py`; routes should authenticate and subscribe, not hand-roll broadcast state.
- Preserve provider-specific health-check behavior in `routers/connections.py`; OpenAI has fallback probes by design.

## APP FACTS

- Lifespan startup flow is: run startup sequence (validate PostgreSQL URL -> run migrations -> seed providers -> enforce profile invariants -> seed user settings -> seed app auth settings -> encrypt endpoint secrets -> seed system header blocklist) -> create shared `httpx.AsyncClient` -> attach/start shared `background_task_manager`.
- `main.py` includes 14 routers; `proxy.py` owns the `/v1*` and `/v1beta*` entrypoint with no `/api` prefix.
- `main.py` middleware bifurcates auth: `/api/*` uses operator session cookies when enabled, `/v1*` uses proxy API keys when enabled.
- `services/background_tasks.py` defines the shared `BackgroundTaskManager`; `main.py` starts it during lifespan and stops it before closing the HTTP client and engine.
- `services/auth_service.py` is the public re-export boundary for verified-email gating, refresh-token family rotation and revocation, password-reset OTP flows, SMTP delivery, proxy-key CRUD, and proxy-key serialization implemented under `services/auth/`.
- `services/webauthn_service.py` is a separate re-export boundary over `services/webauthn/` for passkey registration, authentication, and credential management.
- `routers/config.py` is a shell that delegates to `config_domains/import_export.py` and `config_domains/blocklist.py`.
- `routers/realtime.py` authenticates the websocket, subscribes by `(profile_id, channel)`, and delegates room state to `services/realtime/connection_manager.py`.
- `services/stats_service.py` is a re-export boundary over `services/stats/`; the real query and logging code lives in that package.

## ANTI-PATTERNS

- Do not use request-scoped DB sessions inside streaming finalization.
- Do not bypass header blocklist sanitization after custom headers are merged.
- Do not duplicate auth gating, refresh-token, or proxy-key parsing outside the shared middleware and helpers.
- Do not assume one profile scope model covers both management and proxy traffic.
- Do not reintroduce proxy chaining, unsupported providers, or float money handling.
