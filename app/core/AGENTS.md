# BACKEND CORE KNOWLEDGE BASE

## OVERVIEW
`core/` owns shared backend infrastructure: settings, SQLAlchemy engine and session factories, auth and proxy-key helpers, crypto primitives, Alembic execution, and UTC/time normalization.

## STRUCTURE
```
core/
├── config.py      # BaseSettings loader, CORS/docs flags, PostgreSQL DSN validation
├── database.py    # Async engine, session factory, Declarative Base
├── auth.py        # Access-token creation/decoding, proxy-key parsing, refresh helpers
├── crypto.py      # Secret encryption and opaque-token hashing
├── migrations.py  # Programmatic Alembic upgrade runner
└── time.py        # UTC helpers and datetime normalization
```

## WHERE TO LOOK

- Environment and derived settings: `config.py`
- Engine/session setup and test pooling behavior: `database.py`
- Session JWTs, refresh-token lifetimes, and proxy-key parsing: `auth.py`
- Endpoint secret encryption and token hashing: `crypto.py`
- Startup migration execution: `migrations.py`
- Shared UTC helpers reused by routers and services: `time.py`

## CONVENTIONS

- Use `get_settings()` as the single settings entrypoint; derived lists like `cors_allowed_origins_list` live there.
- Use `AsyncSessionLocal` and `get_engine()` from `database.py` instead of creating ad hoc engines or sessionmakers.
- Reuse `create_access_token()`, `decode_access_token()`, and `extract_proxy_api_key()` instead of duplicating auth parsing.
- Keep migration execution behind `run_migrations()`; `bootstrap/startup.py` owns when it runs.
- Keep time handling UTC-first via `time.py` or `auth.py` helpers.

## ANTI-PATTERNS

- Do not accept non-PostgreSQL DSNs; `ensure_postgresql_database_url()` is a hard gate.
- Do not build new engine instances inside request or service code.
- Do not parse proxy API key headers or access-token payloads ad hoc outside `auth.py`.
- Do not duplicate crypto or timestamp helpers when `core/` already owns them.
