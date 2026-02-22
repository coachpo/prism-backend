# BACKEND KNOWLEDGE BASE

## OVERVIEW

FastAPI async API server — proxy engine for LLM requests with SQLite persistence, load balancing, audit logging, and telemetry.

## STRUCTURE

```
app/
├── main.py              # App factory, lifespan (DB init, httpx pool, seed), CORS, 7 router mounts, /health endpoint
├── dependencies.py      # get_db() — async session with auto-commit/rollback
├── core/
│   ├── config.py        # pydantic-settings: timeouts, DB URL, LB config (reads .env)
│   └── database.py      # Async engine + session factory + Base
├── models/models.py     # 6 ORM models: Provider, ModelConfig, Endpoint, RequestLog, AuditLog, HeaderBlocklistRule (174 lines)
├── schemas/schemas.py   # Pydantic request/response schemas (434 lines, mirrors models/)
├── routers/             # 7 API route handlers (1854 lines total)
│   ├── providers.py     # CRUD /api/providers (list, get, update audit settings) — 47 lines
│   ├── models.py        # CRUD /api/models (list with health stats, get, create, update, delete) — 256 lines
│   ├── endpoints.py     # CRUD /api/models/{id}/endpoints + health check — 284 lines
│   ├── stats.py         # /api/stats/* — request logs, summary, endpoint success rates, batch delete — 111 lines
│   ├── audit.py         # /api/audit/* — audit log list, detail, batch delete — 126 lines
│   ├── config.py        # /api/config/* — full config export/import with validation — 452 lines
│   └── proxy.py         # /v1/{path} + /v1beta/{path} catch-all — core proxy logic — 578 lines
├── services/            # Business logic (931 lines total)
│   ├── proxy_service.py # URL building, auth headers, streaming, body parsing — 312 lines
│   ├── loadbalancer.py  # Strategy selection, proxy→native resolution, failover — 88 lines
│   ├── stats_service.py # Request logging, token extraction (SSE + JSON), aggregation queries — 420 lines
│   └── audit_service.py # Audit log recording, header redaction, body capture/truncation — 111 lines
├── data/
│   └── gateway.db       # SQLite database (auto-created on first run)
└── tests/
    ├── conftest.py      # In-memory SQLite, session-scoped event loop
    └── test_smoke_defect_regressions.py  # Defect-driven regression tests (882 lines)
```

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Add new provider type | `main.py` (seed), `proxy_service.py` (PROVIDER_AUTH), frontend dropdowns | Must update all three |
| Change timeout defaults | `core/config.py` | `connect_timeout=10`, `read_timeout=120`, `write_timeout=30` |
| Add DB column | `models/models.py` + `_add_missing_columns()` in `main.py` | No Alembic — manual ALTER TABLE via PRAGMA |
| Failover behavior | `proxy_service.py` (`FAILOVER_STATUS_CODES`) + `loadbalancer.py` | 429, 500, 502, 503, 529 |
| Health check logic | `routers/endpoints.py` | Sends real chat completion with `max_tokens=1` |
| Request logging | `services/stats_service.py` | `log_request()` returns ID + stores `endpoint_description` |
| Token extraction | `services/stats_service.py` | `extract_token_usage()` — handles both SSE and JSON responses |
| Audit logging | `services/audit_service.py` | `record_audit_log()` — called from `proxy.py` after each request |
| Audit toggle | `routers/providers.py` | `PATCH /api/providers/{id}` — `audit_enabled`, `audit_capture_bodies` |
| Config backup | `routers/config.py` | Export: providers + models + endpoints as JSON; Import: validates + replaces |
| Batch delete logs | `routers/stats.py` + `routers/audit.py` | `DELETE` with `older_than_days` (≥1) or `delete_all=true`; audit also supports `before` |
| Gemini path rewrite | `services/proxy_service.py` | Gemini uses model-in-path pattern (`/models/{model}:generateContent`) |

## CONVENTIONS

- All DB operations are async (`await session.execute(...)`)
- `selectinload()` for eager loading relationships — never lazy load
- Pydantic schemas in `schemas/` must stay in sync with ORM models in `models/`
- Router prefix pattern: `/api/{resource}` for CRUD, `/v1/{path}` + `/v1beta/{path}` for proxy
- `httpx.AsyncClient` lives on `app.state.http_client` — created in lifespan, shared across requests (20 max connections, 5s pool timeout, `follow_redirects=True`)
- Round-robin state is in-memory (`_rr_counters` dict) — resets on restart
- Audit bodies truncated at 64KB with `[TRUNCATED]` marker
- `log_request()` uses an independent `AsyncSessionLocal()` — never the request-scoped session

## ANTI-PATTERNS

- Never use the request-scoped `db` session inside a `StreamingResponse` generator — it's closed after the route returns. Use `AsyncSessionLocal()` directly.
- Never add `content-length` or hop-by-hop headers to upstream requests — `HOP_BY_HOP_HEADERS` frozenset handles this
- `base_url` must not end with `/` — `normalize_base_url()` strips it on create/update
- Don't chain proxy aliases — `get_model_config_with_endpoints()` does exactly one redirect lookup
- Proxy models cannot have endpoints — blocked at endpoint creation and config import
- Native models cannot have `redirect_to` — only proxy models use this field
- Never log raw auth headers — `audit_service.py` redacts `authorization`, `x-api-key`, `x-goog-api-key` and any header matching `key|secret|token|auth` pattern

## TESTING

```bash
./venv/bin/python -m pytest tests/ -v
```

- Framework: pytest + pytest-asyncio (installed in venv, not in requirements.txt)
- Tests: `tests/test_smoke_defect_regressions.py` — 6 test classes:
  - `TestDEF001_LogsSurviveFailoverRollback` — streaming session isolation
  - `TestDEF002_ModelIdRewriting` — proxy alias model field rewrite
  - `TestDEF003_AuthHeaderPerEndpoint` — per-endpoint auth_type override
  - `TestDEF004_FrontendDeleteErrorHandling` — API error propagation
  - `TestDEF005_GeminiPathModelRewrite` — Gemini model-in-path rewriting
  - `TestBatchDeleteValidation` — stats + audit batch delete modes
- Pattern: async tests with `@pytest.mark.asyncio`, mock DB sessions and HTTP clients
- `conftest.py`: sets `DATABASE_URL` to in-memory SQLite, provides session-scoped event loop
- No integration or e2e tests — manual smoke testing via `docs/SMOKE_TEST_PLAN.md`
