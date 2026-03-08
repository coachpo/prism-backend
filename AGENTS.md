# BACKEND KNOWLEDGE BASE

## OVERVIEW
FastAPI backend for Prism's management plane (`/api/*`) and runtime proxy plane (`/v1/*`, `/v1beta/*`). It is async end-to-end, PostgreSQL-backed, migration-on-startup, and split into implementation code under `app/` plus a defect-regression-heavy pytest suite under `tests/`.

## STRUCTURE
```
backend/
├── app/AGENTS.md            # Runtime and management implementation details
├── tests/AGENTS.md          # Test organization, aggregators, DEF/FR conventions
├── app/
├── alembic/
├── requirements.txt
└── docker-compose.yml       # Local PostgreSQL helper
```

## CHILD DOCS

- `app/AGENTS.md`: use for router/service/runtime details once you are inside implementation code.
- `tests/AGENTS.md`: use for defect IDs, aggregators, and container-backed test setup.

## RUNTIME SEMANTICS

- Management endpoints use effective profile scope; runtime proxy endpoints use active profile scope.
- Providers are global seed rows: `openai`, `anthropic`, `gemini`.
- Failover recovery memory is in-process and keyed by `(profile_id, connection_id)`.
- Profile deletion is soft-delete for inactive profiles only; activation is CAS-guarded.

## WHERE TO LOOK

- Startup + shared clients: `app/main.py`
- Scope resolution: `app/dependencies.py`
- Proxy routing flow: `app/routers/proxy.py`, `app/services/loadbalancer.py`, `app/services/proxy_service.py`
- Config import/export + header blocklist: `app/routers/config.py`, `app/routers/config_domains/`
- Health checks + owner lookups: `app/routers/connections.py`, `app/routers/connections_domains/`
- Stats, costing, audit: `app/routers/stats.py`, `app/routers/settings.py`, `app/services/stats_service.py`, `app/services/costing_service.py`, `app/services/audit_service.py`

## COMMANDS

```bash
./venv/bin/python -m pytest tests/ -v
./venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
docker compose up -d postgres
```

## CONVENTIONS

- Keep handlers and services async; use the shared lifespan `httpx.AsyncClient` instead of per-request clients.
- Use `selectinload` for relationship-heavy fetches and keep schema contracts aligned with frontend types.
- Normalize and validate endpoint base URLs before persisting them.
- Keep settings, costing, and profile invariants as startup-enforced behavior, not optional manual steps.

## ANTI-PATTERNS

- Do not reintroduce `round_robin`, unsupported providers, proxy chaining, or float-based money values.
- Do not leak secrets in audit output or allow blocked headers to sneak back in after custom-header merge.
- Do not assume management profile overrides apply to runtime proxy traffic.

## TESTING

- The suite runs against PostgreSQL testcontainers via `tests/conftest.py`; do not assume SQLite semantics.
- `tests/test_smoke_defect_regressions.py` and `tests/test_multi_profile_isolation.py` are the top-level aggregators.
- For end-to-end behavior checks beyond pytest, use `../docs/SMOKE_TEST_PLAN.md` from the repo root.
