# BACKEND KNOWLEDGE BASE

## OVERVIEW

FastAPI async API server — proxy engine for LLM requests with SQLite persistence, load balancing, and telemetry.

## STRUCTURE

```
app/
├── main.py              # App factory, lifespan (DB init, httpx pool, seed), CORS, router mount
├── dependencies.py      # get_db() — async session with auto-commit/rollback
├── core/
│   ├── config.py        # pydantic-settings: timeouts, DB URL, LB config (reads .env)
│   └── database.py      # Async engine + session factory + Base
├── models/models.py     # All 4 ORM models in one file
├── schemas/             # Pydantic request/response schemas (mirror models/)
├── routers/             # API route handlers
│   ├── proxy.py         # /v1/{path} catch-all — the core proxy logic
│   ├── providers.py     # CRUD /api/providers
│   ├── models.py        # CRUD /api/models
│   ├── endpoints.py     # CRUD /api/models/{id}/endpoints + health check
│   └── stats.py         # /api/stats/* — request logs + aggregation
└── services/
    ├── proxy_service.py # URL building, auth headers, streaming, body parsing
    ├── loadbalancer.py  # Strategy selection, proxy→native resolution, failover
    └── stats_service.py # Request logging, token extraction, aggregation queries
```

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Add new provider type | `main.py` (seed), `proxy_service.py` (PROVIDER_AUTH), frontend dropdowns | Must update all three |
| Change timeout defaults | `core/config.py` | `connect_timeout`, `read_timeout`, `write_timeout` |
| Add DB column | `models/models.py` + `_add_missing_columns()` in `main.py` | No Alembic — manual ALTER TABLE |
| Failover behavior | `proxy_service.py` (`FAILOVER_STATUS_CODES`) + `loadbalancer.py` | 429, 500, 502, 503, 529 |
| Health check logic | `routers/endpoints.py` | Sends real chat completion with `max_tokens=1` |
| Request logging | `services/stats_service.py` | `log_request()` + `extract_token_usage()` |

## CONVENTIONS

- All DB operations are async (`await session.execute(...)`)
- `selectinload()` for eager loading relationships — never lazy load
- Pydantic schemas in `schemas/` must stay in sync with ORM models in `models/`
- Router prefix pattern: `/api/{resource}` for CRUD, `/v1/{path}` for proxy
- `httpx.AsyncClient` lives on `app.state.http_client` — created in lifespan, shared across requests
- Round-robin state is in-memory (`_rr_counters` dict) — resets on restart

## ANTI-PATTERNS

- Never use the request-scoped `db` session inside a `StreamingResponse` generator — it's closed after the route returns. Use `AsyncSessionLocal()` directly.
- Never add `content-length` or hop-by-hop headers to upstream requests — `HOP_BY_HOP_HEADERS` frozenset handles this
- `base_url` must not end with `/` — `normalize_base_url()` strips it on create/update
- Don't chain proxy aliases — `get_model_config_with_endpoints()` does exactly one redirect lookup

## TESTING

```bash
./venv/bin/python -m pytest tests/ -v
```

- Framework: pytest + pytest-asyncio (installed in venv, not in requirements.txt)
- Tests: `tests/test_smoke_defect_regressions.py` — defect-driven regression tests (DEF-001 through DEF-004)
- Pattern: async tests with `@pytest.mark.asyncio`, mock DB sessions and HTTP clients
- `conftest.py`: sets `DATABASE_URL` env var, provides session-scoped event loop
- No integration or e2e tests — manual smoke testing via `docs/SMOKE_TEST_PLAN.md`
