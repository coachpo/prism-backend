# BACKEND KNOWLEDGE BASE

## OVERVIEW
Prism's backend owns the management API on `/api/*` and the runtime proxy API on `/v1/*` and `/v1beta/*`. It is uv-managed from `pyproject.toml` and `uv.lock`, runs against PostgreSQL, applies migrations during startup, and owns auth, proxy keys, passkeys, realtime broadcasts, load balancing, costing, and observability.

## STRUCTURE
```
backend/
├── app/AGENTS.md                                # Live runtime map
├── app/bootstrap/AGENTS.md                      # Startup sequence and auth split
├── app/core/AGENTS.md                           # Settings, database, auth helpers, crypto, migrations
├── app/models/AGENTS.md                         # ORM domain ownership
├── app/routers/AGENTS.md                        # 14 router shells and router-domain packages
├── app/schemas/AGENTS.md                        # Contract ownership and export surface
├── app/services/AGENTS.md                       # Service-root boundaries, worker infra, reporting helpers
├── app/services/auth/AGENTS.md                  # Session, email, reset, proxy-key internals
├── app/services/loadbalancer/AGENTS.md          # Planner, state, recovery, events, admin seams
├── app/services/proxy_support/AGENTS.md         # Upstream URL, header, body, transport helpers
├── app/services/realtime/AGENTS.md              # Websocket room-state ownership
├── app/services/stats/AGENTS.md                 # Telemetry, spending, throughput, dashboard helpers
├── app/services/webauthn/AGENTS.md              # Passkey internals
├── tests/AGENTS.md                              # Test map and aggregators
├── tests/multi_profile_isolation/AGENTS.md
├── tests/smoke_defect_regressions/AGENTS.md
├── Dockerfile
├── docker-compose.yml                           # PostgreSQL-only helper on 15432
├── pyproject.toml
└── uv.lock
```

## CHILD DOCS

- `app/AGENTS.md`: use when working inside the live backend runtime.
- `app/bootstrap/AGENTS.md`: startup ordering, seeds, middleware auth split, and shared client creation.
- `app/core/AGENTS.md`: settings, engine and session factories, auth helpers, crypto, and migrations.
- `app/models/AGENTS.md`: ORM domain inventory and the `models.py` export boundary.
- `app/routers/AGENTS.md`: router surface ownership, including dense domain packages such as `proxy_domains/`.
- `app/schemas/AGENTS.md`: schema domain ownership and the `schemas.py` contract surface.
- `app/services/AGENTS.md`: service-root boundaries, background worker lifecycle, cleanup helpers, and reporting entrypoints.
- `tests/AGENTS.md`: PostgreSQL-grounded test hierarchy, aggregators, and focused service coverage.

## RUNTIME FACTS

- `app/main.py` mounts 14 routers: auth, profiles, providers, models, endpoints, connections, stats, audit, loadbalance, config, settings, pricing templates, realtime, and proxy.
- FastAPI lifespan runs the startup sequence, builds one shared `httpx.AsyncClient`, configures the shared `BackgroundTaskManager`, starts it, then shuts those resources down in reverse order.
- Management requests use effective profile scope. Runtime proxy traffic uses the active profile only.
- When auth is enabled, `/api/*` uses operator session cookies while `/v1/*` and `/v1beta/*` use proxy API keys.
- Runtime and observability work now span newer reporting helpers such as `app/services/loadbalance_event_summary.py` and `app/services/stats/model_metrics.py`. Parent docs should point readers to the service children that own those details.

## WHERE TO LOOK

- App assembly, router registration, lifespan startup, and shared infra wiring: `app/main.py`
- Startup sequencing and auth middleware split: `app/bootstrap/startup.py`, `app/bootstrap/auth_middleware.py`
- Management versus runtime scope rules: `app/dependencies.py`
- Router map, including the dense `proxy_domains/` runtime package: `app/routers/AGENTS.md`, `app/routers/`
- Service boundaries, reporting helpers, and cleanup ownership: `app/services/AGENTS.md`, `app/services/loadbalance_event_summary.py`, `app/services/stats/model_metrics.py`
- Local workflow and packaging: `pyproject.toml`, `uv.lock`, `../start.sh`, `Dockerfile`, `docker-compose.yml`

## CONVENTIONS

- Keep backend workflow and commands uv-native.
- Keep parent docs summary-oriented and push package detail down into the existing child AGENTS files.
- Keep shared worker lifecycle in `app/main.py` and `app/services/background_tasks.py`.
- Keep management auth assumptions separate from runtime proxy auth and routing semantics.

## ANTI-PATTERNS

- Do not invent unsupported providers, routes, or CI jobs.
- Do not reintroduce manual venv or `pip install` setup language.
- Do not describe `docker-compose.yml` as a full stack definition. It provisions PostgreSQL only.
- Do not duplicate leaf-level router, service, schema, or test internals here when the child docs already own them.

## NOTES

- `../start.sh` runs backend setup with `uv sync --locked --python "$BACKEND_PYTHON_BIN"` and uses port `18000` for local launcher mode.
- `docker-compose.yml` exposes PostgreSQL on host port `15432` for backend development and tests.
