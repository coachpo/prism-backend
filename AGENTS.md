# BACKEND KNOWLEDGE BASE

## OVERVIEW

FastAPI async API server — proxy engine for LLM requests with PostgreSQL persistence, failover load balancing, audit logging, per-request costing, and telemetry.

## STRUCTURE

```
app/
├── main.py              # App factory, lifespan (DB init, httpx pool, seed), CORS, 10 router mounts, /health endpoint (213 lines)
├── dependencies.py      # get_db() — async session with auto-commit/rollback
├── core/
│   ├── config.py        # pydantic-settings: timeouts, DB URL, LB config (reads .env)
│   └── database.py      # Async engine + session factory + Base
├── models/models.py     # 10 ORM models (477 lines): Profile, Provider, ModelConfig, Endpoint, Connection, RequestLog, UserSetting, EndpointFxRateSetting, HeaderBlocklistRule, AuditLog
├── schemas/schemas.py   # Pydantic request/response schemas (736 lines, mirrors models/)
├── routers/             # 10 API route handlers
│   ├── providers.py     # CRUD /api/providers (list, get, update audit settings) — 47 lines
│   ├── profiles.py      # CRUD /api/profiles (list, get active, create, update, delete, activate) — 211 lines
│   ├── models.py        # CRUD /api/models (list with health stats, get, create, update, delete) — 276 lines
│   ├── endpoints.py     # CRUD /api/models/{id}/endpoints + health check + owner route — 318 lines
│   ├── connections.py   # CRUD /api/models/{id}/connections + health check + owner route — 514 lines
│   ├── stats.py         # /api/stats/* — request logs, summary, endpoint success rates, spending report, batch delete — 159 lines
│   ├── audit.py         # /api/audit/* — audit log list, detail, batch delete — 126 lines
│   ├── config.py        # /api/config/* — config export/import (v2/v3) + header blocklist CRUD — 628 lines
│   ├── settings.py      # /api/settings/costing — currency + FX rate mappings — 125 lines
│   └── proxy.py         # /v1/{path} + /v1beta/{path} catch-all — core proxy + costing logic — 662 lines
├── services/            # Business logic (1826 lines total)
│   ├── proxy_service.py # URL building, auth headers, header blocklist, streaming, body parsing — 312 lines
│   ├── loadbalancer.py  # Strategy selection, proxy→native resolution, failover recovery — 116 lines
│   ├── stats_service.py # Request logging, token extraction (SSE + JSON), spending reports — 918 lines
│   ├── audit_service.py # Audit log recording, header redaction, body capture/truncation — 111 lines
│   └── costing_service.py # Cost computation (5 token types × prices), FX conversion, pricing snapshots — 300 lines
├── data/
└── tests/
    ├── conftest.py      # PostgreSQL testcontainer bootstrap + session-scoped event loop
    └── test_smoke_defect_regressions.py  # Defect-driven regression tests (2571 lines)
```

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Add new provider type | `main.py` (seed), `proxy_service.py` (PROVIDER_AUTH), frontend dropdowns | Must update all three |
| Change timeout defaults | `core/config.py` | `connect_timeout=10`, `read_timeout=120`, `write_timeout=30` |
| Add DB column | `models/models.py` + new Alembic revision in `alembic/versions/` | Alembic is source of truth |
| Failover behavior | `proxy_service.py` (`FAILOVER_STATUS_CODES`) + `loadbalancer.py` | 403, 429, 500, 502, 503, 529 |
| Failover recovery | `loadbalancer.py` | `_recovery_state` dict, `build_attempt_plan()`, cooldown-based probing |
| Health check logic | `routers/endpoints.py` | Sends real chat completion with `max_tokens=1` |
| Request logging | `services/stats_service.py` | `log_request()` returns ID + stores `endpoint_description` + costing fields |
| Token extraction | `services/stats_service.py` | `extract_token_usage()` — handles SSE + JSON for OpenAI/Anthropic/Gemini |
| Cost computation | `services/costing_service.py` | `compute_cost_fields()` — 5 token types, FX conversion, pricing snapshots |
| Spending reports | `routers/stats.py` + `services/stats_service.py` | `/api/stats/spending` with 7 group-by modes + time presets |
| Costing settings | `routers/settings.py` | `/api/settings/costing` — report currency + per-endpoint FX rates |
| Audit logging | `services/audit_service.py` | `record_audit_log()` — called from `proxy.py` after each request |
| Audit toggle | `routers/providers.py` | `PATCH /api/providers/{id}` — `audit_enabled`, `audit_capture_bodies` |
|| Config backup | `routers/config.py` | Export v6: providers + models + endpoints + connections + user_settings + blocklist rules + FX mappings |
| Batch delete logs | `routers/stats.py` + `routers/audit.py` | `DELETE` with `older_than_days` (≥1) or `delete_all=true`; audit also supports `before` |
| Gemini path rewrite | `services/proxy_service.py` | Gemini uses model-in-path pattern (`/models/{model}:generateContent`) |
| Header blocklist | `routers/config.py` + `proxy_service.py` | System (seeded) + user rules; auth headers protected from blocklist |
| Endpoint owner | `routers/endpoints.py` | `GET /api/endpoints/{id}/owner` — returns model_id for endpoint navigation |
|| Connection management | `routers/connections.py` | Model-scoped routing config, health checks, pricing |
|| Profile management | `routers/profiles.py` | CRUD for profiles, activate/deactivate, soft delete (max 10) |

## CONVENTIONS

- All DB operations are async (`await session.execute(...)`)
- `selectinload()` for eager loading relationships — never lazy load
- Pydantic schemas in `schemas/` must stay in sync with ORM models in `models/`
- Router prefix pattern: `/api/{resource}` for CRUD, `/v1/{path}` + `/v1beta/{path}` for proxy
- `httpx.AsyncClient` lives on `app.state.http_client` — created in lifespan, shared across requests (20 max connections, 5s pool timeout, `follow_redirects=True`)
- Failover recovery state is in-memory (`_recovery_state` dict) — resets on restart
- Audit bodies truncated at 64KB with `[TRUNCATED]` marker
- `log_request()` uses an independent `AsyncSessionLocal()` — never the request-scoped session
- Costs stored as micros (int64) — `total_cost_micros / 1_000_000 = decimal amount`
- Pricing snapshots stored in request_logs for audit trail (unit, prices, policy, config version)
- Config export/import version 6 — includes user_settings, endpoint_fx_mappings, header_blocklist_rules
- Startup applies Alembic migrations programmatically (`run_migrations()` in `core/migrations.py`)

## ANTI-PATTERNS

- Never use the request-scoped `db` session inside a `StreamingResponse` generator — it's closed after the route returns. Use `AsyncSessionLocal()` directly.
- Never add `content-length` or hop-by-hop headers to upstream requests — `HOP_BY_HOP_HEADERS` frozenset handles this
- `base_url` must not end with `/` — `normalize_base_url()` strips it on create/update
- Don't chain proxy aliases — `get_model_config_with_connections()` does exactly one redirect lookup
- Proxy models cannot have connections — blocked at connection creation and config import
- Native models cannot have `redirect_to` — only proxy models use this field
- Never log raw auth headers — `audit_service.py` redacts `authorization`, `x-api-key`, `x-goog-api-key` and any header matching `key|secret|token|auth` pattern
- Don't use `round_robin` LB strategy — removed, auto-migrated to `failover` on startup
- Don't store costs as floats — always micros (int64) to avoid precision loss

## TESTING

```bash
./venv/bin/python -m pytest tests/ -v
```
- Framework: pytest + pytest-asyncio (installed in venv, not in requirements.txt)
- Tests: `tests/test_smoke_defect_regressions.py` — 12 test classes (1651 lines):
  - `TestDEF001_LogsSurviveFailoverRollback` — streaming session isolation
  - `TestDEF002_ModelIdRewriting` — proxy alias model field rewrite
  - `TestDEF003_AuthHeaderPerEndpoint` — per-endpoint auth_type override
  - `TestDEF004_FrontendDeleteErrorHandling` — API error propagation
  - `TestDEF005_GeminiPathModelRewrite` — Gemini model-in-path rewriting
  - `TestDEF006_ConfigExportImportFieldCoverage` — config export/import field preservation
  - `TestDEF007_EndpointIdentityInLogs` — log_request returns ID and stores endpoint_description
  - `TestDEF008_CacheCreationPricing` — cache creation pricing computation
  - `TestBatchDeleteValidation` — stats + audit batch delete modes
  - `TestFailoverRecoveryFieldValidation` — failover recovery field validation
  - `TestEndpointOwnerRoute` — endpoint owner route
  - `TestHeaderBlocklist` — header blocklist feature
- Pattern: async tests with `@pytest.mark.asyncio`, mock DB sessions and HTTP clients
- `conftest.py`: starts postgres testcontainer, sets `DATABASE_URL`, runs Alembic `upgrade head`, provides session-scoped event loop
- No integration or e2e tests — manual smoke testing via `docs/SMOKE_TEST_PLAN.md`
