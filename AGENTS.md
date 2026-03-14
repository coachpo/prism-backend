# BACKEND KNOWLEDGE BASE

## OVERVIEW
FastAPI backend for Prism's management plane (`/api/*`) and runtime proxy plane (`/v1/*`, `/v1beta/*`). It is async end-to-end, PostgreSQL-backed, migration-on-startup, and now owns operator auth, password reset, proxy API keys, pricing templates, and profile-scoped admin flows in addition to routing and observability.

## STRUCTURE
```
backend/
├── app/AGENTS.md                   # Runtime and management implementation details
├── app/services/stats/AGENTS.md    # Telemetry and spending query cluster
├── tests/AGENTS.md                 # Test organization, aggregators, DEF/FR conventions
├── alembic/
├── requirements.txt
└── docker-compose.yml              # Local PostgreSQL helper
```

## CHILD DOCS

- `app/AGENTS.md`: use for router, service, schema, and startup behavior once you are inside implementation code.
- `tests/AGENTS.md`: use for defect IDs, aggregators, startup/auth regressions, and container-backed test setup.

## RUNTIME SEMANTICS

- Management endpoints use effective profile scope; runtime proxy endpoints use active profile scope.
- When auth is enabled, management uses session cookies while runtime proxy traffic requires a proxy API key header.
- Providers are global seed rows: `openai`, `anthropic`, `gemini`.
- Failover recovery memory is in-process and keyed by `(profile_id, connection_id)`.
- Profile deletion is soft-delete for inactive profiles only; activation is CAS-guarded.

## WHERE TO LOOK

- Startup + shared clients: `app/main.py`
- Scope resolution: `app/dependencies.py`
- Operator auth, verified email, password reset, proxy API keys: `app/routers/auth.py`, `app/routers/settings.py`, `app/services/auth_service.py`, `app/core/auth.py`
- Proxy routing flow: `app/routers/proxy.py`, `app/services/loadbalancer.py`, `app/services/proxy_service.py`
- Config import/export + header blocklist: `app/routers/config.py`, `app/routers/config_domains/`
- Health checks + owner lookups + runtime blocklist merge: `app/routers/connections.py`, `app/routers/connections_domains/`
- Stats, costing, and audit: `app/routers/stats.py`, `app/routers/pricing_templates.py`, `app/services/stats_service.py`, `app/services/costing_service.py`, `app/services/audit_service.py`

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
- Keep SMTP, verified-email, password-reset, refresh-token, and proxy-key logic centralized behind `app/services/auth_service.py` (the re-export boundary over `app/services/auth/`) and `app/core/auth.py`.
- Keep settings, costing, and profile invariants as startup-enforced behavior, not optional manual steps.
- Preserve provider-specific health-check behavior in `app/routers/connections.py`; OpenAI has fallback probes by design.

## ANTI-PATTERNS

- Do not reintroduce `round_robin`, unsupported providers, proxy chaining, or float-based money values.
- Do not leak secrets in audit output or allow blocked headers to sneak back in after custom-header merge.
- Do not let management auth assumptions leak into proxy runtime auth; they are separate enforcement paths.
- Do not assume management profile overrides apply to runtime proxy traffic.

## TESTING

- The suite runs against PostgreSQL testcontainers via `tests/conftest.py`; do not assume SQLite semantics.
- `tests/test_smoke_defect_regressions.py` and `tests/test_multi_profile_isolation.py` are the top-level aggregators.
- Auth, password-reset, email-delivery, and proxy-key regressions cluster under `tests/smoke_defect_regressions/test_startup_cases/`, especially `tests/smoke_defect_regressions/test_startup_cases/auth_management_flows_tests.py`.
- For end-to-end behavior checks beyond pytest, use `../docs/SMOKE_TEST_PLAN.md` from the repo root.
