# BACKEND KNOWLEDGE BASE

## OVERVIEW
FastAPI backend for Prism's management plane, `/api/*`, and runtime proxy plane, `/v1/*` and `/v1beta/*`. It is async end-to-end, uv-managed from `pyproject.toml` and `uv.lock`, PostgreSQL-backed, migration-on-startup, and owns auth, proxy keys, passkeys, realtime broadcasts, loadbalance events, costing, and observability.

## STRUCTURE
```
backend/
├── app/AGENTS.md                                # Live implementation map
├── app/bootstrap/AGENTS.md                      # Startup sequence and auth split
├── app/core/AGENTS.md                           # Config, DB, auth, crypto, migrations
├── app/models/AGENTS.md                         # ORM domains
├── app/routers/AGENTS.md                        # 14 router shells and domain folders
├── app/schemas/AGENTS.md                        # Pydantic contract ownership
├── app/services/AGENTS.md                       # Service-root boundaries and worker infra
├── app/services/auth/AGENTS.md                  # Session, email, reset, proxy-key internals
├── app/services/loadbalancer_support/AGENTS.md
├── app/services/proxy_support/AGENTS.md
├── app/services/realtime/AGENTS.md
├── app/services/stats/AGENTS.md
├── app/services/webauthn/AGENTS.md
├── tests/AGENTS.md                              # Test map and aggregators
├── tests/multi_profile_isolation/AGENTS.md
├── tests/smoke_defect_regressions/AGENTS.md
├── Dockerfile
├── docker-compose.yml                           # Local PostgreSQL helper on 15432
├── pyproject.toml
└── uv.lock
```

## CHILD DOCS

- `app/AGENTS.md`: use once you are inside backend implementation code.
- `app/bootstrap/AGENTS.md`: startup sequence, seeded defaults, auth middleware, and public auth exceptions.
- `app/routers/AGENTS.md`: API surface layout and router-domain ownership.
- `app/services/AGENTS.md`: service-root public boundaries, cleanup helpers, and background worker wiring.
- `tests/AGENTS.md`: test organization, aggregators, and container-backed suite facts.

## RUNTIME FACTS

- `app/main.py` mounts 14 routers: auth, profiles, providers, models, endpoints, connections, stats, audit, loadbalance, config, settings, pricing templates, realtime, and proxy.
- FastAPI lifespan runs the startup sequence, creates the shared `httpx.AsyncClient`, configures and starts the shared `BackgroundTaskManager`, and shuts them down in reverse order.
- Management uses effective profile scope. Runtime proxy traffic uses the active profile only.
- When auth is enabled, management uses session cookies while runtime proxy traffic uses proxy API keys.
- Providers remain the seeded global rows `openai`, `anthropic`, and `gemini`.

## WHERE TO LOOK

- Startup, router registration, and lifespan-managed worker setup: `app/main.py`
- Startup sequencing and auth bifurcation: `app/bootstrap/startup.py`, `app/bootstrap/auth_middleware.py`
- Scope resolution: `app/dependencies.py`
- Auth, proxy keys, and passkeys: `app/routers/auth.py`, `app/routers/settings.py`, `app/services/auth_service.py`, `app/services/webauthn_service.py`
- Runtime routing and failover: `app/routers/proxy.py`, `app/routers/proxy_domains/`, `app/services/loadbalancer.py`, `app/services/proxy_service.py`
- Realtime transport and broadcasts: `app/routers/realtime.py`, `app/services/realtime/connection_manager.py`
- Packaging and local launcher behavior: `pyproject.toml`, `uv.lock`, `../start.sh`, `Dockerfile`, `docker-compose.yml`

## COMMANDS

```bash
uv sync --locked
uv run pytest tests/ -v
uv run prism-backend --reload
docker compose up -d postgres
```

## CONVENTIONS

- Keep backend flows async end-to-end.
- Treat `pyproject.toml` and `uv.lock` as the dependency source of truth.
- Keep top-level routers thin when a matching domain folder already owns the logic.
- Keep shared worker lifecycle in `app/main.py` and `app/services/background_tasks.py`.
- Use child docs for deeper router, service, schema, and test details instead of repeating them here.

## ANTI-PATTERNS

- Do not reintroduce unsupported providers, proxy chaining, round-robin routing, or float money values.
- Do not reintroduce manual venv or `pip install` setup language.
- Do not let management auth assumptions leak into runtime proxy auth.
- Do not treat `docker-compose.yml` as a full stack definition. It provisions PostgreSQL only.

## NOTES

- `../start.sh` syncs with `uv sync --locked --python "$BACKEND_PYTHON_BIN"` and runs the backend on port `18000` in local launcher mode.
- `docker-compose.yml` exposes PostgreSQL on host port `15432` for local backend work.
