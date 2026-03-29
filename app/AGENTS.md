# BACKEND APP KNOWLEDGE BASE

## OVERVIEW
`app/` is the live Prism backend runtime. It owns FastAPI app assembly, lifespan startup and teardown, middleware auth splitting, profile-scope dependencies, router mounts, schema export boundaries, and the shared `httpx.AsyncClient`, `BackgroundTaskManager`, and monitoring scheduler.

## STRUCTURE
```
app/
├── main.py                                                   # App assembly, router mounts, lifespan wiring
├── alembic/AGENTS.md                                         # Packaged Alembic runtime and schema source of truth
├── bootstrap/AGENTS.md                                       # Startup sequence, seeds, middleware auth split
├── dependencies.py                                           # Effective-profile and active-profile boundary
├── core/AGENTS.md                                            # Settings, database, auth helpers, crypto, migrations
├── models/AGENTS.md                                          # ORM model ownership
├── routers/AGENTS.md                                         # Thin route shells and router-domain package map
├── routers/monitoring.py                                     # Monitoring overview, vendor, model, and manual-probe routes
├── routers/shared/AGENTS.md                                  # Reusable router-layer helpers
├── routers/{auth,config,endpoints,models,pricing_templates,profiles,settings,stats}_domains/AGENTS.md
├── routers/connections_domains/AGENTS.md                     # Dense connection-management leaf
├── routers/proxy_domains/AGENTS.md                           # Dense runtime proxy leaf
├── schemas/AGENTS.md                                         # Contract ownership and `schemas.py` export surface
├── services/AGENTS.md                                        # Public service boundaries, worker infra, reporting areas
├── services/monitoring/                                      # Probe runner, scheduler, queries, routing feedback
├── services/monitoring_service.py                            # Public monitoring facade
└── services/{auth,loadbalancer,proxy_support,realtime,stats,webauthn}/AGENTS.md
```

## CHILD DOCS

- `alembic/AGENTS.md`: packaged migration runtime, script template, and revision source of truth.
- `bootstrap/AGENTS.md`: startup sequence, seeds, middleware auth split, and shared client creation.
- `core/AGENTS.md`: settings, engine and session factories, crypto, auth helpers, and migrations.
- `models/AGENTS.md`: ORM model ownership and domain splits.
- `routers/AGENTS.md`: router shells, standalone routers, and leaf handoff.
- `routers/shared/AGENTS.md`: reusable ordering, endpoint-record, and profile-row helpers shared across routers.
- `routers/{auth,config,endpoints,models,pricing_templates,profiles,settings,stats}_domains/AGENTS.md`: management router-domain leaves.
- `routers/connections_domains/AGENTS.md`, `routers/proxy_domains/AGENTS.md`: the densest router packages.
- `schemas/AGENTS.md`: contract ownership and the `schemas.py` boundary.
- `services/AGENTS.md` plus `services/{auth,loadbalancer,proxy_support,realtime,stats,webauthn}/AGENTS.md`: service facades, worker infrastructure, and deeper package detail.

## APP FACTS

- `main.py` mounts the backend routers, including `/api/monitoring`, builds shared app state, and exposes `/health`.
- FastAPI lifespan runs `bootstrap.run_startup_sequence()`, builds the shared `httpx` client, configures `background_task_manager` from `background_task_worker_count`, starts it, then starts the backend-owned `MonitoringScheduler`.
- Lifespan shutdown also stops the monitoring scheduler, stops dashboard-update lifecycle helpers, closes the shared HTTP client, and disposes the SQLAlchemy engine.
- Middleware auth stays split by plane: `/api/*` uses operator session rules, while `/v1/*` and `/v1beta/*` use proxy-key rules.
- Routers stay intentionally thin. Dense logic belongs in router-domain packages and service modules.
- Service-level reporting includes helpers such as `services/loadbalance_event_summary.py` and `services/stats/model_metrics.py`, with deeper ownership documented in `services/AGENTS.md` and `services/stats/AGENTS.md`.

## WHERE TO LOOK

- App assembly, router mounts, lifespan startup, and shared infra ownership: `main.py`
- Startup sequence, adaptive-routing preset seeding, and monitoring cadence defaults: `bootstrap/startup.py`
- Migration packaging, env wiring, and revision layout: `alembic/AGENTS.md`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/`
- Management profile overrides versus runtime active-profile routing: `dependencies.py`
- Router surface, shared router helpers, monitoring routes, and router-domain leaf docs: `routers/AGENTS.md`, `routers/shared/AGENTS.md`, `routers/monitoring.py`, `routers/`
- Contract exports and schema ownership: `schemas/AGENTS.md`, `schemas/schemas.py`
- Shared worker lifecycle, monitoring facade, and service public boundaries: `services/AGENTS.md`, `services/background_tasks.py`, `services/monitoring_service.py`
- Reporting helpers for load-balance events and model metrics: `services/loadbalance_event_summary.py`, `services/stats/model_metrics.py`
- Websocket auth and room-state handoff: `routers/realtime.py`, `services/realtime/connection_manager.py`

## CONVENTIONS

- Keep app-owned infrastructure in `main.py`. Feature code should consume `app.state.http_client` and `app.state.background_task_manager`.
- Keep profile-scope rules in `dependencies.py` instead of parsing headers inside handlers.
- Keep parent AGENTS files focused on package maps and ownership boundaries, not leaf implementation details.
- Keep auth, runtime proxy routing, realtime fanout, and stats assembly inside their existing service or domain boundaries.
- Keep Alembic revisions as the schema source of truth and use `core/migrations.py` for the programmatic migration seam.

## ANTI-PATTERNS

- Do not duplicate startup-sequence detail across route or service docs when `bootstrap/` already owns it.
- Do not treat management profile overrides and runtime proxy profile resolution as the same model.
- Do not bypass `services/realtime/connection_manager.py` with ad hoc websocket room state in routers.
- Do not import schema leaf modules directly when `schemas/schemas.py` already defines the supported surface.
- Do not stale-claim that most router-domain packages are covered only by `routers/AGENTS.md`; the management `*_domains/` packages now have their own leaf docs.
