# BACKEND KNOWLEDGE BASE

## OVERVIEW
FastAPI backend for Prism's management plane (`/api/*`) and runtime proxy plane (`/v1/*`, `/v1beta/*`). It is async end-to-end, PostgreSQL-backed, migration-on-startup, and owns operator auth, password reset, proxy API keys, passkeys, realtime broadcasts, loadbalance events, pricing templates, and profile-scoped admin flows in addition to routing and observability.

## STRUCTURE
```
backend/
├── app/AGENTS.md                                # Runtime and management implementation details
├── app/routers/AGENTS.md                        # API surface and domain-shell layout
├── app/services/auth/AGENTS.md                  # Session, email, password reset, proxy keys
├── app/services/loadbalancer_support/AGENTS.md  # Recovery state, attempts, event helpers
├── app/services/proxy_support/AGENTS.md         # Upstream request/header/url/transport helpers
├── app/services/stats/AGENTS.md                 # Telemetry and spending query cluster
├── tests/AGENTS.md                              # Test organization, aggregators, realtime/service coverage
├── alembic/
├── requirements.txt
└── docker-compose.yml              # Local PostgreSQL helper
```

## CHILD DOCS

- `app/AGENTS.md`: use for router, service, schema, and startup behavior once you are inside implementation code.
- `app/routers/AGENTS.md`: use when working in the API surface layer or deciding where new handlers belong.
- `app/services/auth/AGENTS.md`: use for auth/session/OTP/proxy-key internals behind `auth_service.py`.
- `app/services/loadbalancer_support/AGENTS.md`: use for recovery-state mutation, attempt planning, and loadbalance event helpers.
- `app/services/proxy_support/AGENTS.md`: use for upstream URL/header/body/compression/transport helpers behind `proxy_service.py`.
- `tests/AGENTS.md`: use for defect IDs, aggregators, startup/auth regressions, realtime coverage, and container-backed test setup.

## RUNTIME SEMANTICS

- Management endpoints use effective profile scope; runtime proxy endpoints use active profile scope.
- When auth is enabled, management uses session cookies while runtime proxy traffic requires a proxy API key header.
- Providers are global seed rows: `openai`, `anthropic`, `gemini`.
- Failover recovery memory is in-process and keyed by `(profile_id, connection_id)`.
- Loadbalance events are also persisted and queryable through `/api/loadbalance/*`.
- Profile deletion is soft-delete for inactive profiles only; activation is CAS-guarded.

## WHERE TO LOOK

- Startup + shared clients: `app/main.py`
- Startup sequence + auth bifurcation: `app/bootstrap/startup.py`, `app/bootstrap/auth_middleware.py`
- Scope resolution: `app/dependencies.py`
- Operator auth, verified email, password reset, proxy API keys, passkeys: `app/routers/auth.py`, `app/routers/settings.py`, `app/services/auth_service.py`, `app/services/webauthn_service.py`, `app/services/webauthn/`, `app/core/auth.py`
- Proxy routing flow: `app/routers/proxy.py`, `app/routers/proxy_domains/`, `app/services/loadbalancer.py`, `app/services/proxy_service.py`
- Config import/export + header blocklist: `app/routers/config.py`, `app/routers/config_domains/`
- Health checks + owner lookups + runtime blocklist merge: `app/routers/connections.py`, `app/routers/connections_domains/`
- Stats, costing, audit, and loadbalance events: `app/routers/stats.py`, `app/routers/audit.py`, `app/routers/loadbalance.py`, `app/routers/pricing_templates.py`, `app/services/stats_service.py`, `app/services/costing_service.py`, `app/services/audit_service.py`
- Realtime websocket transport: `app/routers/realtime.py`, `app/services/realtime/connection_manager.py`

## COMMANDS

```bash
./venv/bin/python -m pytest tests/ -v
./venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
docker compose up -d postgres
```

## CONVENTIONS

- Keep handlers and services async; use the shared lifespan `httpx.AsyncClient` instead of per-request clients.
- Keep top-level routers thin when a matching domain folder exists; heavy request logic belongs in `app/routers/*_domains/` or `app/services/*`.
- Use `selectinload` for relationship-heavy fetches and keep schema contracts aligned with frontend types.
- Normalize and validate endpoint base URLs before persisting them.
- Keep SMTP, verified-email, password-reset, refresh-token, and proxy-key logic centralized behind `app/services/auth_service.py` (the re-export boundary over `app/services/auth/`) and `app/core/auth.py`.
- Keep passkey registration/authentication/credential lifecycle behind `app/services/webauthn_service.py` and `app/services/webauthn/` rather than folding it into the auth package.
- Keep realtime payload emission inside stats/audit/loadbalance services and the websocket connection manager rather than in route handlers.
- Keep settings, costing, and profile invariants as startup-enforced behavior, not optional manual steps.
- Preserve provider-specific health-check behavior in `app/routers/connections.py`; OpenAI has fallback probes by design.

## ANTI-PATTERNS

- Do not reintroduce `round_robin`, unsupported providers, proxy chaining, or float-based money values.
- Do not leak secrets in audit output or allow blocked headers to sneak back in after custom-header merge.
- Do not let management auth assumptions leak into proxy runtime auth; they are separate enforcement paths.
- Do not assume management profile overrides apply to runtime proxy traffic.
- Do not bypass `app/services/auth_service.py`, `app/services/loadbalancer.py`, or `app/services/proxy_service.py` public boundaries when the split package already owns the behavior.

## TESTING

- The suite runs against PostgreSQL testcontainers via `tests/conftest.py`; do not assume SQLite semantics.
- `tests/test_smoke_defect_regressions.py` and `tests/test_multi_profile_isolation.py` are the top-level aggregators.
- Auth, password-reset, email-delivery, and proxy-key regressions cluster under `tests/smoke_defect_regressions/test_startup_cases/`, especially `tests/smoke_defect_regressions/test_startup_cases/auth_management_flows_tests.py`.
- Realtime broadcasting is covered by `tests/test_realtime_broadcast.py`; WebAuthn service coverage lives in `tests/services/test_webauthn_service.py`.
- For end-to-end behavior checks beyond pytest, use `../docs/SMOKE_TEST_PLAN.md` from the repo root.
