# BACKEND TEST SUITE KNOWLEDGE BASE

## OVERVIEW
`tests/` is a PostgreSQL-backed regression suite. It is organized around defect regressions and profile-isolation guarantees, not around unit-vs-integration labels.

## STRUCTURE
```
tests/
├── conftest.py                       # PostgreSQL testcontainer + Alembic bootstrap
├── test_smoke_defect_regressions.py  # Top-level DEF aggregator
├── test_multi_profile_isolation.py   # Top-level isolation aggregator
├── smoke_defect_regressions/         # Proxy/config/costing/startup regression domains
└── multi_profile_isolation/          # Lifecycle/scoping/runtime/observability domains
```

## WHERE TO LOOK

- Container lifecycle and migrated DB setup: `conftest.py`
- DEF regression exports: `test_smoke_defect_regressions.py`
- Profile-isolation exports: `test_multi_profile_isolation.py`
- Proxy/failover regressions: `smoke_defect_regressions/`, `smoke_defect_regressions/test_proxy_cases/`
- Config/costing/startup regressions: `smoke_defect_regressions/test_config_cases/`, `smoke_defect_regressions/test_costing_cases/`, `smoke_defect_regressions/test_startup_cases/`
- Auth session, password reset, secret handling, and proxy-key cases: `smoke_defect_regressions/test_startup_cases/auth_management_flows_tests.py`
- Profile isolation details: `multi_profile_isolation/`, `multi_profile_isolation/AGENTS.md`

## CHILD DOCS

- `smoke_defect_regressions/AGENTS.md`: defect-regression domain map and aggregator expectations.
- `multi_profile_isolation/AGENTS.md`: selected-vs-active profile isolation test map.

## COMMANDS

```bash
./venv/bin/python -m pytest tests/ -v
./venv/bin/python -m pytest tests/test_smoke_defect_regressions.py -v
./venv/bin/python -m pytest tests/test_multi_profile_isolation.py -v
./venv/bin/python -m pytest tests/ -k "DEF008" -v
```

## CONVENTIONS

- `conftest.py` starts `postgres:16-alpine`, converts the sync URL to `asyncpg`, and applies Alembic migrations before tests run.
- Smoke regressions use `TestDEF###_*` naming and are grouped by semantic domain, then re-exported through aggregator files.
- Multi-profile tests group by concern (`lifecycle`, `scoping`, `runtime`, `observability`, `config import/export`) and re-export through `test_multi_profile_isolation.py`.
- Startup smoke cases now also cover auth middleware, session lifecycle, password reset, proxy API key acceptance, and secret sanitization.
- Aggregator files are part of the suite shape; when you add a new case, update the relevant aggregator.

## ANTI-PATTERNS

- Do not assume SQLite or in-memory DB behavior; these tests exercise PostgreSQL semantics.
- Do not skip migrations in test setup.
- Do not reuse old DEF IDs for new regressions.
- Do not scatter one-off test files outside the domain folders when a matching domain already exists.
