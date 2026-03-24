# BACKEND APP KNOWLEDGE BASE

## OVERVIEW
`app/` is the live backend runtime. It owns FastAPI app assembly, the startup sequence, middleware auth split, profile-scope dependencies, 14 mounted routers, schema exports, service boundaries, and the lifespan-managed shared `httpx.AsyncClient` plus `BackgroundTaskManager`.

## STRUCTURE
```
app/
├── main.py                                   # App factory, 14 router mounts, auth middleware, lifespan wiring
├── bootstrap/AGENTS.md                       # Startup sequence, seeds, middleware auth split
├── dependencies.py                           # Effective-profile and active-profile dependency boundary
├── core/AGENTS.md                            # Config, database, auth helpers, crypto, migrations, time helpers
├── models/AGENTS.md                          # ORM models and domain splits
├── routers/AGENTS.md                         # Thin route shells and parent-owned router-domain map
├── schemas/AGENTS.md                         # Contract domains plus `schemas.py` export surface
├── services/AGENTS.md                        # Public service facades, worker infrastructure, cleanup helpers
├── services/auth/AGENTS.md                   # Session, email, password reset, proxy-key internals
├── services/loadbalancer/AGENTS.md           # Split planner, state, recovery, events, and admin seams
├── services/proxy_support/AGENTS.md          # Upstream URL, header, body, transport helpers
├── services/realtime/AGENTS.md               # Connection manager room state and broadcasts
├── services/stats/AGENTS.md                  # Telemetry, spending, throughput, dashboard payload queries
└── services/webauthn/AGENTS.md               # Passkey registration, authentication, credential management
```

## WHERE TO LOOK

- App assembly, router mount list, and lifespan worker startup: `main.py`
- Startup sequence and seed ordering: `bootstrap/startup.py`, `bootstrap/AGENTS.md`
- Management profile overrides versus runtime active profile: `dependencies.py`
- Router surface and dense router-domain folders: `routers/AGENTS.md`, `routers/`
- Contract exports and domain ownership: `schemas/AGENTS.md`, `schemas/schemas.py`
- Shared worker lifecycle and service public boundaries: `services/AGENTS.md`, `services/background_tasks.py`
- Websocket auth, subscription flow, and room-state handoff: `routers/realtime.py`, `services/realtime/connection_manager.py`

## CHILD DOCS

- `bootstrap/AGENTS.md`: startup sequence, seed defaults, auth bifurcation, and middleware behavior.
- `core/AGENTS.md`: settings, engine/session factories, crypto, auth helpers, and migrations.
- `models/AGENTS.md`: ORM model ownership and domain splits.
- `routers/AGENTS.md`: 14 router shells, current domain folders, and scope conventions.
- `schemas/AGENTS.md`: contract ownership and the `schemas.py` re-export boundary.
- `services/AGENTS.md`: public service facades, background task infrastructure, and cleanup helpers.
- `services/auth/AGENTS.md`, `services/loadbalancer/AGENTS.md`, `services/proxy_support/AGENTS.md`, `services/realtime/AGENTS.md`, `services/stats/AGENTS.md`, `services/webauthn/AGENTS.md`: use these for deeper service package detail.

## APP FACTS

- `main.py` mounts 14 routers: auth, profiles, providers, models, endpoints, connections, stats, audit, loadbalance, config, settings, pricing templates, realtime, and proxy.
- FastAPI lifespan runs `bootstrap.run_startup_sequence()`, builds the shared `httpx` client, configures `background_task_manager` with `background_task_worker_count`, then starts it.
- Lifespan shutdown stops dashboard-update lifecycle helpers, shuts down the background task manager, closes the shared HTTP client, and disposes the SQLAlchemy engine.
- Middleware auth stays split by plane: `/api/*` uses operator session rules, `/v1*` and `/v1beta*` use proxy-key rules.
- Routers are intentionally thin shells. Dense request logic lives in the existing `*_domains/` folders and service modules.
- `routers/realtime.py` owns websocket authentication and subscribe or unsubscribe flow, while `services/realtime/connection_manager.py` owns room membership and broadcast state.
- `schemas/schemas.py` is the explicit import surface for routers and other callers. Domain files stay behind that boundary.

## CONVENTIONS

- Keep app-owned infrastructure in `main.py`. Feature code should consume `app.state.http_client` and `app.state.background_task_manager`, not create parallel infrastructure.
- Keep profile-scope rules in `dependencies.py` instead of parsing headers inside handlers.
- Keep parent AGENTS files responsible for router-domain and schema-domain maps. Don't add new AGENTS docs under those folders unless the hierarchy explicitly needs one.
- Keep auth, proxy routing, realtime fanout, and stats assembly inside their existing service or domain boundaries instead of drifting back into route shells.

## ANTI-PATTERNS

- Do not duplicate startup sequence details across route or service docs when `bootstrap/` already owns them.
- Do not treat management profile overrides and runtime proxy profile resolution as the same model.
- Do not bypass `services/realtime/connection_manager.py` with ad hoc websocket room state in routers.
- Do not import schema leaf modules directly when `schemas/schemas.py` already defines the supported surface.
