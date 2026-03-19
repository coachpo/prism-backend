# BACKEND TEST SUITE KNOWLEDGE BASE

## OVERVIEW
`tests/` is a PostgreSQL-backed regression suite executed from the uv-managed backend environment. It is organized around defect regressions and profile-isolation guarantees, then supplemented by focused coverage for realtime broadcasting and service-level behavior such as WebAuthn and the shared background task manager.

## STRUCTURE
```
tests/
├── conftest.py                        # PostgreSQL testcontainer + Alembic bootstrap
├── test_smoke_defect_regressions.py   # Top-level DEF aggregator
├── test_multi_profile_isolation.py    # Top-level isolation aggregator
├── test_realtime_broadcast.py         # WebSocket channels and `dashboard.update` payload coverage
├── services/                          # Focused service coverage such as WebAuthn and background task manager
├── smoke_defect_regressions/
│   └── AGENTS.md                      # Proxy, config, costing, startup, and standalone DEF domain map
└── multi_profile_isolation/
    └── AGENTS.md                      # Lifecycle, scoping, runtime, observability, and import/export map
```

## WHERE TO LOOK

- Container lifecycle and migrated DB setup: `conftest.py`
- Test dependency declaration and lockfile: `../pyproject.toml`, `../uv.lock`
- DEF regression exports: `test_smoke_defect_regressions.py`
- Profile-isolation exports: `test_multi_profile_isolation.py`
- Realtime broadcasting and channel fanout: `test_realtime_broadcast.py`
- WebAuthn service coverage: `services/test_webauthn_service.py`
- Background worker lifecycle and retry semantics: `services/test_background_tasks.py`
- Proxy and failover regressions: `smoke_defect_regressions/`, `smoke_defect_regressions/test_proxy_cases/`
- Config and costing regressions: `smoke_defect_regressions/test_config_cases/`, `smoke_defect_regressions/test_costing_cases/`
- Auth, password reset, email delivery, and proxy-key regressions: `smoke_defect_regressions/test_startup_cases/auth_management_flows_tests.py`
- Logging, endpoint-owner mapping, and connection-default startup coverage: `smoke_defect_regressions/test_startup_cases/logging_endpoint_owner_and_connection_defaults_tests.py`
- CORS, request-log batch delete, stats timezone normalization, model-health, and loadbalance migration startup cases: `smoke_defect_regressions/test_startup_cases/`
- Profile isolation details: `multi_profile_isolation/`, `multi_profile_isolation/AGENTS.md`

## CHILD DOCS

- `smoke_defect_regressions/AGENTS.md`: defect-regression domain map and aggregator expectations.
- `multi_profile_isolation/AGENTS.md`: selected-vs-active profile isolation test map.

## COMMANDS

```bash
uv sync --locked
uv run pytest tests/ -v
uv run pytest tests/test_smoke_defect_regressions.py -v
uv run pytest tests/test_multi_profile_isolation.py -v
uv run pytest tests/ -k "DEF008" -v
```

## CONVENTIONS

- `conftest.py` starts `postgres:16-alpine`, converts the sync URL to `asyncpg`, and applies Alembic migrations before tests run.
- Run the suite from the backend root through `uv run`; the dev dependency group in `../pyproject.toml` owns pytest, pytest-asyncio, pytest-cov, and testcontainers.
- Smoke regressions use `TestDEF###_*` naming and are grouped by semantic domain, then re-exported through top-level and domain-level aggregator files.
- Multi-profile tests group by concern (`lifecycle`, `scoping`, `runtime`, `observability`, `config import/export`) and re-export through `test_multi_profile_isolation.py`.
- Startup smoke cases now use explicit concern file names for auth, CORS, stats/batch delete, model health, loadbalance migration, and proxy-key generation.
- Realtime and service-focused coverage stays in top-level files or `services/` when it does not fit the DEF or isolation hierarchies.
- `services/` currently covers WebAuthn, throughput helpers, crypto helpers, and the shared `BackgroundTaskManager` lifecycle.
- Aggregator files are part of the suite shape; when you add a new case, update every relevant aggregator layer.

## ANTI-PATTERNS

- Do not assume SQLite or in-memory DB behavior; these tests exercise PostgreSQL semantics.
- Do not run pytest from a hand-managed interpreter or stale virtualenv when `uv run` is the supported path.
- Do not skip migrations in test setup.
- Do not reuse old DEF IDs for new regressions.
- Do not scatter one-off test files outside the domain folders when a matching domain already exists.
- Do not add a new smoke leaf file without wiring it into the domain and top-level aggregators when that path applies.

## NOTES

- Docker must be available locally because `testcontainers[postgres]` boots the PostgreSQL fixture container from `conftest.py`.
- `../docs/SMOKE_TEST_PLAN.md` is the manual counterpart for end-to-end validation; it should agree with the current `uv run pytest ...` preflight flow.
