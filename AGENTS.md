# BACKEND KNOWLEDGE BASE

## OVERVIEW
FastAPI async API server for Prism's management plane (`/api/*`) and data plane (`/v1/*`, `/v1beta/*`).
Persists state in PostgreSQL via async SQLAlchemy, applies Alembic migrations on startup, and provides failover routing, costing, telemetry, and audit logging.

## STRUCTURE
```
app/
|- main.py                 # Lifespan startup: validate DB URL, run migrations, seed providers/settings/blocklist, create shared httpx client
|- dependencies.py         # DB session dependency + active/effective profile dependencies
|- core/
|  |- config.py            # Environment-backed settings and timeout values
|  |- database.py          # Async engine/session factory/Base
|  `- migrations.py        # Programmatic Alembic runner used at startup
|- models/models.py        # ORM models: Profile, Provider, ModelConfig, Endpoint, Connection, RequestLog, UserSetting, EndpointFxRateSetting, HeaderBlocklistRule, AuditLog
|- schemas/schemas.py      # Pydantic request/response contracts
|- routers/
|  |- profiles.py          # /api/profiles CRUD + CAS activation
|  |- providers.py         # /api/providers list/get/patch audit flags
|  |- models.py            # /api/models CRUD + /by-endpoint/{endpoint_id}
|  |- endpoints.py         # /api/endpoints CRUD + /api/endpoints/connections
|  |- connections.py       # /api/models/{id}/connections CRUD + health-check + owner
|  |- stats.py             # /api/stats requests/summary/success-rates/spending + batch delete
|  |- audit.py             # /api/audit/logs list/detail/batch delete
|  |- settings.py          # /api/settings/costing get/update
|  |- config.py            # /api/config export/import + header blocklist CRUD
|  `- proxy.py             # /v1/{path} and /v1beta/{path} catch-all proxy handlers
|- services/
|  |- loadbalancer.py      # Model resolution, connection selection, failover recovery state
|  |- proxy_service.py     # URL/header construction, blocklist application, upstream forwarding helpers
|  |- stats_service.py     # Request log persistence + token extraction + aggregate/spending queries
|  |- costing_service.py   # Micros-based costing and FX conversion
|  `- audit_service.py     # Audit logging + header redaction + body truncation
`- tests/
   |- conftest.py          # PostgreSQL test bootstrap + migrations
   `- test_smoke_defect_regressions.py
```

## RUNTIME SEMANTICS
- Management endpoints (`/api/*`) use effective profile scope: explicit `X-Profile-Id` on profile-scoped routes (`/api/profiles/*` are global).
- Proxy endpoints (`/v1/*`, `/v1beta/*`) always use active profile scope via `get_active_profile_id`.
- Profiles are soft-deletable only when inactive; create is capped at 10 non-deleted profiles; activation is CAS-guarded.
- Providers are global shared seed rows (`openai`, `anthropic`, `gemini`) and not profile-scoped.

## KEY BACKEND FACTS
- Startup order in lifespan: validate PostgreSQL URL -> run migrations -> seed providers -> seed user settings -> seed system header blocklist rules -> create shared `httpx.AsyncClient`.
- Failover trigger statuses are `403, 429, 500, 502, 503, 529` (`FAILOVER_STATUS_CODES`).
- Failover recovery state is in-memory and keyed by `(profile_id, connection_id)`; resets on process restart.
- Config export/import canonical contract is `version: 1` with logical references (`endpoint_ref`, `connection_ref`) and replace-mode import.
- Spending API supports `group_by`: `none`, `day`, `week`, `month`, `provider`, `model`, `endpoint`, `model_endpoint`.
- Costing stores integer micros in logs (`*_micros`), with pricing snapshot fields for auditability.
- FX mappings are keyed by `(model_id, endpoint_id)` in backend settings APIs.
- `Connection.description` is a synonym-backed field over the `description` column (ORM attribute is `name` with synonym).
- `request_logs` and `audit_logs` both persist `endpoint_id` plus endpoint snapshot fields.
- `user_settings` includes `timezone_preference` and report currency settings.

## WHERE TO LOOK
- Proxy routing flow: `app/routers/proxy.py`, `app/services/loadbalancer.py`, `app/services/proxy_service.py`
- Profile semantics: `app/dependencies.py`, `app/routers/profiles.py`
- Config export/import and blocklist: `app/routers/config.py`
- Costing and spending: `app/routers/settings.py`, `app/routers/stats.py`, `app/services/costing_service.py`, `app/services/stats_service.py`
- Health checks and ownership routes: `app/routers/connections.py`, `app/routers/endpoints.py`
- Audit capture: `app/services/audit_service.py`, `app/routers/audit.py`

## CONVENTIONS
- Keep request handlers and services async end-to-end.
- Use `selectinload` for relationship-heavy fetches in routers/services.
- Do not rely on request-scoped DB sessions in streaming finalization; streaming cleanup uses independent sessions in services.
- Normalize endpoint base URLs on create/update and validate against repeated `/vN/vN` segments.
- Apply blocklist sanitization after custom header merge so blocked headers cannot be reintroduced.
- Treat schema contracts in `schemas/schemas.py` as source-of-truth for API docs and frontend types.

## ANTI-PATTERNS
- Do not add unsupported providers without coordinated backend+frontend updates.
- Do not reintroduce `round_robin` load balancing behavior.
- Do not store monetary values as floats; use micros integer fields only.
- Do not allow proxy chaining (`proxy -> proxy`) or proxy connections.
- Do not leak raw secrets in audit headers; redaction rules must remain enforced.

## TESTING
```bash
./venv/bin/python -m pytest tests/ -v
```
- Test suite is defect-regression heavy and runs against PostgreSQL test setup in `tests/conftest.py`.
- For manual behavior validation, follow scenarios in `docs/SMOKE_TEST_PLAN.md`.
