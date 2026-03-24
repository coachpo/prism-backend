# BACKEND APP KNOWLEDGE BASE

## OVERVIEW
`app/` is the live Prism backend runtime. It owns FastAPI app assembly, the lifespan-managed startup and teardown path, middleware auth splitting, profile-scope dependencies, 14 mounted routers, schema export boundaries, and the shared `httpx.AsyncClient` plus shared `BackgroundTaskManager`.

## STRUCTURE
```
app/
├── main.py                                   # App assembly, 14 router mounts, lifespan wiring
├── bootstrap/AGENTS.md                       # Startup sequence, seeds, middleware auth split
├── dependencies.py                           # Effective-profile and active-profile boundary
├── core/AGENTS.md                            # Settings, database, auth helpers, crypto, migrations
├── models/AGENTS.md                          # ORM model ownership
├── routers/AGENTS.md                         # Thin route shells and router-domain package map
├── schemas/AGENTS.md                         # Contract ownership and `schemas.py` export surface
├── services/AGENTS.md                        # Public service boundaries, worker infra, reporting areas
├── services/auth/AGENTS.md                   # Session, email, password reset, proxy-key internals
├── services/loadbalancer/AGENTS.md           # Planner, state, recovery, events, admin seams
├── services/proxy_support/AGENTS.md          # Upstream URL, header, body, transport helpers
├── services/realtime/AGENTS.md               # Connection manager room state and broadcasts
├── services/stats/AGENTS.md                  # Telemetry, spending, throughput, dashboard helpers
└── services/webauthn/AGENTS.md               # Passkey registration and authentication internals
```

## CHILD DOCS

- `bootstrap/AGENTS.md`: startup sequence, seeds, middleware auth split, and shared client creation.
- `core/AGENTS.md`: settings, engine and session factories, crypto, auth helpers, and migrations.
- `models/AGENTS.md`: ORM model ownership and domain splits.
- `routers/AGENTS.md`: 14 router shells, current domain folders, and scope conventions.
- `schemas/AGENTS.md`: contract ownership and the `schemas.py` re-export boundary.
- `services/AGENTS.md`: public service facades, shared worker infrastructure, cleanup helpers, and newer reporting surfaces.
- `services/auth/AGENTS.md`, `services/loadbalancer/AGENTS.md`, `services/proxy_support/AGENTS.md`, `services/realtime/AGENTS.md`, `services/stats/AGENTS.md`, `services/webauthn/AGENTS.md`: use these for deeper package detail.

## APP FACTS

- `main.py` mounts 14 routers: auth, profiles, providers, models, endpoints, connections, stats, audit, loadbalance, config, settings, pricing templates, realtime, and proxy.
- FastAPI lifespan runs `bootstrap.run_startup_sequence()`, builds the shared `httpx` client, configures `background_task_manager` from `background_task_worker_count`, starts it, and tears it down during shutdown.
- Lifespan shutdown also stops dashboard-update lifecycle helpers, closes the shared HTTP client, and disposes the SQLAlchemy engine.
- Middleware auth stays split by plane: `/api/*` uses operator session rules, while `/v1/*` and `/v1beta/*` use proxy-key rules.
- Routers stay intentionally thin. Dense logic belongs in the existing `*_domains/` folders and service modules.
- `routers/proxy_domains/` is now a dense runtime package with eight Python files. Parent docs should acknowledge it and point readers to `routers/AGENTS.md` instead of duplicating its leaf details here.
- Service-level reporting now includes helpers such as `services/loadbalance_event_summary.py` and `services/stats/model_metrics.py`, with deeper ownership documented in `services/AGENTS.md` and `services/stats/AGENTS.md`.

## WHERE TO LOOK

- App assembly, router mount list, lifespan startup, and shared infra ownership: `main.py`
- Startup sequence and seed ordering: `bootstrap/startup.py`, `bootstrap/AGENTS.md`
- Management profile overrides versus runtime active-profile routing: `dependencies.py`
- Router surface and dense domain packages, especially `proxy_domains/`: `routers/AGENTS.md`, `routers/`
- Contract exports and schema ownership: `schemas/AGENTS.md`, `schemas/schemas.py`
- Shared worker lifecycle and service public boundaries: `services/AGENTS.md`, `services/background_tasks.py`
- Reporting helpers for load-balance events and model metrics: `services/loadbalance_event_summary.py`, `services/stats/model_metrics.py`
- Websocket auth and room-state handoff: `routers/realtime.py`, `services/realtime/connection_manager.py`

## CONVENTIONS

- Keep app-owned infrastructure in `main.py`. Feature code should consume `app.state.http_client` and `app.state.background_task_manager`.
- Keep profile-scope rules in `dependencies.py` instead of parsing headers inside handlers.
- Keep parent AGENTS files focused on package maps and ownership boundaries, not leaf implementation details.
- Keep auth, runtime proxy routing, realtime fanout, and stats assembly inside their existing service or domain boundaries.

## ANTI-PATTERNS

- Do not duplicate startup-sequence detail across route or service docs when `bootstrap/` already owns it.
- Do not treat management profile overrides and runtime proxy profile resolution as the same model.
- Do not bypass `services/realtime/connection_manager.py` with ad hoc websocket room state in routers.
- Do not import schema leaf modules directly when `schemas/schemas.py` already defines the supported surface.
