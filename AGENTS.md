# BACKEND KNOWLEDGE BASE

## OVERVIEW
Prism's backend owns the management API on `/api/*` and the runtime proxy API on `/v1/*` and `/v1beta/*`. It is uv-managed from `pyproject.toml` and `uv.lock`, packages `app*`, runs against PostgreSQL, applies Alembic migrations during startup, and owns auth, proxy keys, passkeys, realtime updates, load balancing, costing, and observability.

## STRUCTURE
```
backend/
├── app/AGENTS.md                                # Live runtime map
├── app/alembic/                                 # Packaged Alembic env + revisions; schema source of truth
├── app/bootstrap/AGENTS.md                      # Startup sequence and auth split
├── app/core/AGENTS.md                           # Settings, database, auth helpers, crypto, migrations
├── app/models/AGENTS.md                         # ORM domain ownership and `models.py` boundary
├── app/routers/AGENTS.md                        # Router map and domain packages
├── app/routers/proxy_domains/AGENTS.md          # Runtime proxy execution package
├── app/routers/connections_domains/AGENTS.md    # Connection router leaf map
├── app/schemas/AGENTS.md                        # Contract ownership and `schemas.py` boundary
├── app/services/AGENTS.md                       # Service-root boundaries, worker infra, reporting helpers
├── app/services/auth/AGENTS.md                  # Session, email, reset, proxy-key internals
├── app/services/loadbalancer/AGENTS.md          # Planner, state, recovery, events, admin seams
├── app/services/proxy_support/AGENTS.md         # Upstream URL, header, body, transport helpers
├── app/services/realtime/AGENTS.md              # Websocket room-state ownership
├── app/services/stats/AGENTS.md                 # Telemetry, spending, throughput, dashboard helpers
├── app/services/webauthn/AGENTS.md              # Passkey internals
├── tests/AGENTS.md                              # Test map and aggregators
├── tests/services/AGENTS.md                     # Focused service-test handoff
├── tests/multi_profile_isolation/AGENTS.md      # Cross-profile containment hierarchy
├── tests/smoke_defect_regressions/AGENTS.md     # DEF hierarchy map and leaf ownership
├── alembic.ini                                  # Root Alembic CLI config pointing at `app/alembic`
├── Dockerfile
├── docker-compose.yml                           # PostgreSQL-only helper on 15432
├── pyproject.toml                               # Runtime deps and `prism-backend` console script
└── uv.lock
```

## CHILD DOCS

- `app/AGENTS.md`: live runtime map.
- `app/bootstrap/AGENTS.md`: startup sequence, seed defaults, and middleware auth split.
- `app/core/AGENTS.md`: settings, database, auth helpers, crypto, and migrations.
- `app/models/AGENTS.md`: ORM domains and the `models.py` export boundary.
- `app/routers/AGENTS.md`: router map and domain package handoffs.
- `app/routers/proxy_domains/AGENTS.md`: runtime proxy setup, attempt execution, streaming, and reporting.
- `app/routers/connections_domains/AGENTS.md`: connection CRUD, health checks, and owner helpers.
- `app/schemas/AGENTS.md`: schema contract boundary and `schemas.py` surface.
- `app/services/AGENTS.md`: service-root boundaries, worker lifecycle, and reporting helpers.
- `app/services/auth/AGENTS.md`, `app/services/loadbalancer/AGENTS.md`, `app/services/proxy_support/AGENTS.md`, `app/services/realtime/AGENTS.md`, `app/services/stats/AGENTS.md`, `app/services/webauthn/AGENTS.md`: deeper package docs.
- `tests/AGENTS.md`, `tests/services/AGENTS.md`, `tests/smoke_defect_regressions/AGENTS.md`, and `tests/multi_profile_isolation/AGENTS.md`: test hierarchy and suite leaves.

## RUNTIME FACTS

- `pyproject.toml` exposes `prism-backend = "app.main:main"` as the CLI entrypoint.
- `app/main.py` builds the FastAPI app, installs CORS and auth middleware, mounts routers, and exposes `/health`.
- FastAPI lifespan runs `bootstrap.run_startup_sequence()`, builds one shared `httpx.AsyncClient`, configures the shared `BackgroundTaskManager`, starts it, then shuts those resources down in reverse order while also stopping dashboard-update lifecycle helpers.
- Multi-worker CLI startup pre-runs `run_startup_sequence()` and sets `PRISM_SKIP_STARTUP_SEQUENCE=1` before worker imports `app.main:app`.
- Management requests use effective profile scope. Runtime proxy traffic uses the active profile only.
- When auth is enabled, `/api/*` uses operator session cookies while `/v1/*` and `/v1beta/*` use proxy API keys.
- `services/realtime/connection_manager.py` is the single source of truth for live websocket rooms, and `services/stats/logging.py` owns `dashboard.update` payload emission.

## WHERE TO LOOK

- App assembly, router registration, lifespan startup, and shared infra wiring: `app/main.py`
- Startup sequencing, provider and profile seeding, auth settings, header blocklist defaults, and shared HTTP client builder: `app/bootstrap/startup.py`
- Management versus runtime scope rules: `app/dependencies.py`
- Router map and runtime proxy package: `app/routers/AGENTS.md`, `app/routers/proxy_domains/AGENTS.md`
- Public schema and model import boundaries: `app/schemas/AGENTS.md`, `app/models/AGENTS.md`
- Shared worker lifecycle, realtime room state, dashboard updates, and reporting helpers: `app/services/AGENTS.md`, `app/services/background_tasks.py`, `app/services/realtime/connection_manager.py`, `app/services/stats/logging.py`
- Migration source of truth: `alembic.ini`, `app/alembic/`, `app/core/migrations.py`
- Backend test hierarchy and suite leaves: `tests/AGENTS.md`, `tests/services/AGENTS.md`, `tests/smoke_defect_regressions/AGENTS.md`, `tests/multi_profile_isolation/AGENTS.md`

## CONVENTIONS

- Keep backend workflow and commands uv-native.
- Keep parent docs summary-oriented and push package detail down into child AGENTS files.
- Keep app-owned shared infrastructure in `app/main.py`; feature code should consume `app.state.http_client` and `app.state.background_task_manager`.
- Keep routers thin. Dense logic belongs in `*_domains/`, `proxy_domains/`, or service modules.
- Use `app.schemas.schemas`, `app.models.models`, and the service-root `*_service.py` modules as the supported re-export boundaries.
- Keep management auth and profile rules separate from runtime proxy auth and routing semantics.

## ANTI-PATTERNS

- Do not invent unsupported providers, provider path families, routes, or CI jobs.
- Do not describe schema state as coming from ORM models or startup side effects; Alembic revisions under `app/alembic/` are the source of truth.
- Do not reintroduce manual venv or `pip install` setup language.
- Do not describe `docker-compose.yml` as a full stack definition. It provisions PostgreSQL only.
- Do not import schema, model, or service leaf modules when a documented re-export boundary exists.
- Do not blur management effective-profile behavior with runtime active-profile routing or proxy-key auth.
- Do not duplicate leaf-level router, service, schema, or test internals here when the child docs already own them.
