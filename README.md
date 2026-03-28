# Prism Backend

**FastAPI-based proxy engine with async request routing, load balancing, and telemetry.**

This is the backend component of Prism, handling LLM API-family routing, load balancing, health tracking, and data persistence.

---

## Architecture

- **Framework**: FastAPI with async/await throughout
- **Database**: PostgreSQL with async SQLAlchemy (`asyncpg`) + Alembic migrations
- **HTTP Client**: httpx.AsyncClient for upstream requests
- **Streaming**: SSE pass-through with async generators

---

## Project Structure

```
backend/
├── app/
│   ├── alembic/                     # Packaged Alembic env + revisions used at runtime
│   ├── alembic.ini                  # Package-local Alembic config for startup migrations
│   ├── main.py                      # CLI entrypoint, FastAPI app wiring, lifespan, and shared worker setup
│   ├── bootstrap/                   # Startup sequence and auth middleware helpers
│   ├── core/database.py             # SQLAlchemy async engine + session factory
│   ├── models/
│   │   ├── domains/                 # Split ORM model domains
│   │   └── models.py                # Explicit model export boundary
│   ├── routers/
│   │   ├── auth_domains/            # Session, password reset, and WebAuthn handlers
│   │   ├── config_domains/          # Config import/export + blocklist helpers
│   │   ├── connections_domains/     # Connection CRUD, health, and owner helpers
│   │   ├── endpoints_domains/       # Endpoint CRUD/reorder/duplicate helpers
│   │   ├── models_domains/          # Model CRUD/query helpers
│   │   ├── pricing_templates_domains/ # Pricing template CRUD + usage helpers
│   │   ├── profiles_domains/        # Profile lifecycle and activation helpers
│   │   ├── settings_domains/        # Auth settings, costing, timezone, and proxy-key helpers
│   │   ├── proxy_domains/           # Proxy setup, attempts, streaming, and outcome-reporting helpers
│   │   ├── shared/                  # Router-layer shared helpers for profile rows and ordering
│   │   ├── stats_domains/           # Request log, metrics, spending, and throughput handlers
│   │   ├── settings.py              # Thin settings route shell
│   │   ├── vendors.py               # Thin vendor CRUD shell
│   │   ├── models.py                # Thin model route shell
│   │   ├── endpoints.py             # Thin endpoint route shell
│   │   ├── connections.py           # Thin connection route shell
│   │   ├── stats.py                 # Thin stats route shell
│   │   ├── audit.py                 # Audit log queries
│   │   ├── config.py                # Thin config route shell
│   │   ├── loadbalance.py           # Thin strategy + event route shell
│   │   ├── pricing_templates.py     # Thin pricing template route shell
│   │   ├── profiles.py              # Thin profile route shell
│   │   ├── realtime.py              # WebSocket auth and channel subscription router
│   │   └── proxy.py                 # Thin /v1/* and /v1beta/* proxy router
│   └── services/
│       ├── auth/                    # Split auth, email, session, and proxy-key services
│       ├── loadbalancer/            # Split planner, state, recovery, events, and admin seams
│       ├── proxy_support/           # URL/header/body/transport helpers
│       ├── realtime/                # WebSocket connection manager helpers
│       ├── stats/                   # Telemetry query and logging helpers
│       ├── webauthn/                # Passkey registration/authentication internals
│       ├── auth_service.py          # Auth public re-export boundary
│       ├── background_tasks.py      # Lifespan-managed async worker queue
│       ├── proxy_service.py         # Upstream request forwarding
│       ├── stats_service.py         # Stats public re-export boundary
│       ├── audit_service.py         # Audit log writing with header redaction
│       ├── costing_service.py       # Token costing and FX helpers
│       ├── webauthn_service.py      # Passkey public re-export boundary
│       ├── user_settings.py         # Shared profile user-settings access helpers
│       ├── background_cleanup.py    # Request/audit retention cleanup helpers
│       ├── loadbalance_cleanup.py   # Loadbalance-event retention cleanup helpers
│       └── profile_invariants.py    # Active/default profile enforcement
├── alembic.ini                      # Root Alembic CLI config pointing at `app/alembic`
├── docker-compose.yml               # Local PostgreSQL provisioning
├── pyproject.toml                   # Runtime deps, dev dependency group, and console script
├── tests/                           # Pytest test suite
└── AGENTS.md                        # Backend knowledge base
```

---

## Setup

### Prerequisites
- Python 3.13+
- uv

### Installation

```bash
# Create or update the uv-managed environment
uv sync --locked
```

If your Python 3.13 interpreter is exposed under a different command name, set
`BACKEND_PYTHON_BIN=<your-python-3.13-command>` before using `../start.sh`.

### Running

```bash
# Development server with auto-reload
uv run prism-backend --reload

# Production defaults (4 workers, proxy headers enabled)
uv run prism-backend
```

The API will be available at:
- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc
- **OpenAPI JSON**: http://localhost:8000/openapi.json

---

## Testing

```bash
# Run all tests
uv run pytest tests/

# Run with coverage
uv run pytest tests/ --cov=app --cov-report=html

# Run specific test file
uv run pytest tests/test_smoke_defect_regressions.py -v

# Run one focused service suite
uv run pytest tests/services/test_loadbalancer_planner.py -v
```

### Test layout

- Most backend pytest runs require Docker because `tests/conftest.py` starts PostgreSQL through `testcontainers` and applies Alembic migrations before the session begins.
- The small DB-free allowlist currently includes `tests/services/test_background_tasks.py`, `tests/test_backend_version_metadata.py`, and `tests/test_realtime_broadcast.py`.
- `tests/test_smoke_defect_regressions.py` re-exports the named DEF smoke corpus, including the startup-side health contract and the proxy-side runtime-target guards.
- `tests/test_multi_profile_isolation.py` owns selected-profile versus active-runtime containment coverage.
- `tests/test_realtime_broadcast.py` owns websocket auth, subscribe/unsubscribe, and `dashboard.update` coverage.
- `tests/services/` keeps service-focused auth, loadbalancer, stats, streaming, throughput, and WebAuthn tests out of the smoke and isolation trees.

`pyproject.toml` is the dependency declaration source and `uv.lock` pins the resolved
environment. Local development uses `uv sync --locked`, while production images install
runtime dependencies with `uv sync --locked --no-dev`.

---

## Configuration

### Environment Variables

- `PORT` - Server port for `prism-backend` (default: `8000`)
- `PRISM_BACKEND_WORKERS` - Worker count when `--reload` is off (default: `4`)
- `DATABASE_URL` - PostgreSQL DSN for direct backend runs. This is required unless a parent launcher or container environment already exports it.

For direct runs, other common settings come from `../.env.example`, especially `HOST`, `CORS_ALLOWED_ORIGINS`, auth or WebAuthn settings, and SMTP settings for email verification or password reset flows.

### Database

Schema migrations are managed with Alembic and applied automatically on backend startup (`upgrade head`).

For local development, run PostgreSQL via Docker Compose:

```bash
docker compose up -d postgres
```

The checked-in compose file exposes PostgreSQL on `localhost:15432`. The root
launcher uses that same compose file and wires `DATABASE_URL` accordingly.
If you run `uv run prism-backend` directly against the checked-in compose file,
point `DATABASE_URL` at `postgresql+asyncpg://prism:prism@localhost:15432/prism`.

---

## API Overview

### Management API

- `GET /api/vendors` - List vendor metadata
- `GET /api/vendors/{id}` - Get one vendor record
- `PATCH /api/vendors/{id}` - Update vendor metadata

- `GET /api/models` - List all models
- `POST /api/models` - Create model
- `PUT /api/models/{id}` - Update model
- `DELETE /api/models/{id}` - Delete model

- `GET /api/endpoints` - List profile-scoped endpoints
- `POST /api/endpoints` - Create profile-scoped endpoint
- `PUT /api/endpoints/{id}` - Update profile-scoped endpoint
- `DELETE /api/endpoints/{id}` - Delete profile-scoped endpoint

- `GET /api/models/{id}/connections` - List connections for model
- `POST /api/models/{id}/connections` - Create connection for model
- `PUT /api/connections/{id}` - Update connection
- `DELETE /api/connections/{id}` - Delete connection
- `POST /api/connections/{id}/health-check` - Manual health check

- `GET /api/stats/requests` - Request logs with filters
- `GET /api/stats/summary` - Aggregated statistics
- `GET /api/stats/connection-success-rates` - Per-connection success rates

- `GET /api/audit/logs` - Audit logs with filters
- `GET /api/audit/logs/{id}` - Audit log detail

- `GET /api/loadbalance/current-state` - List persisted current-state rows for a model
- `POST /api/loadbalance/current-state/{connection_id}/reset` - Reset persisted current-state for one connection
- `GET /api/loadbalance/events` - List loadbalance transition events for a model
- `GET /api/loadbalance/events/{id}` - Get loadbalance event detail
- `DELETE /api/loadbalance/events` - Batch-delete loadbalance events

- `GET /api/config/export` - Export full configuration
- `POST /api/config/import` - Import configuration

### Proxy API

- `POST /v1/*` - OpenAI runtime proxy routes
- `POST /v1/messages*` - Anthropic runtime proxy routes
- `POST /v1beta/models/*` - Gemini native runtime proxy routes

Prism accepts API-family-native path families only: OpenAI models on `/v1/*`, Anthropic models on `/v1/messages`, and Gemini models on `/v1beta/models/*`.

---

## Key Concepts

### Native vs Proxy Models

- **Native**: Real models with their own connection configurations and load balancing
- **Proxy**: Alias models that forward to a native model (for ID resolution) — no connections of their own

### Load Balancing Strategies

- **single**: Always use the first active connection (priority 0)
- **round-robin**: Rotate the primary attempt across active connections in order
- **failover**: Try connections in priority order with adaptive auto-recovery (failure threshold, exponential backoff, jitter, and auth-like cooldown handling)
- **fill-first**: Strict priority spillover after the active target is exhausted

### Success Rate Tracking

Connections display a success rate badge computed from `request_logs` data (last 24h):
- ≥98% = green
- 75-98% = yellow
- <75% = red
- No data = gray (N/A)

### Audit Logging

Optional per-vendor request/response body capture with automatic header redaction:
- Redacted headers: `authorization`, `x-api-key`, `x-goog-api-key`, and any header matching `key|secret|token|auth` pattern
- Body capture can be disabled per vendor

---

## Development Notes

### Async Everywhere

All database operations and HTTP requests use async/await. Never use blocking I/O.

### Streaming Logs

Streaming responses use a separate `AsyncSessionLocal()` in the generator's `finally` block because the request-scoped session is closed before streaming completes.

### API-Family Auth Headers

API-family-specific auth headers are built in `proxy_service.py`:
- OpenAI: `Authorization: Bearer {api_key}`
- Anthropic: `x-api-key: {api_key}`
- Gemini: `Authorization: Bearer {api_key}`

### Schema Migrations

Alembic migrations are the source of truth. Create revisions with `uv run alembic revision --autogenerate -m "message"` and apply with `uv run alembic upgrade head`.

---

## Troubleshooting

### Database Connection Errors

If startup fails to connect to PostgreSQL, verify your `DATABASE_URL` and ensure the Postgres service is healthy (`docker compose ps`).

### Import Errors

Make sure you're running from the `backend/` directory and the uv environment is synced (`uv sync --locked`).

### Port Already in Use

Change the port with `--port` or set the `PORT` environment variable.

---

## Contributing

This repo does not currently include a shared `CONTRIBUTING.md`; follow the backend
module conventions in `AGENTS.md` and the surrounding code.

---

## License

This repo does not currently include a standalone `LICENSE` file.
