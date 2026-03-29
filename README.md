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

## Project structure

```text
backend/
├── app/
│   ├── alembic/                     # Packaged Alembic env + revisions used at runtime
│   ├── alembic.ini                  # Package-local Alembic config for startup migrations
│   ├── main.py                      # CLI entrypoint, FastAPI app wiring, lifespan, and shared worker setup
│   ├── bootstrap/                   # Startup sequence and auth middleware helpers
│   ├── core/                        # Settings, database, auth, crypto, migrations, time, version helpers
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
uv sync --locked
```

If your Python 3.13 interpreter is exposed under a different command name, set `BACKEND_PYTHON_BIN=<your-python-3.13-command>` before using `../start.sh`.

### Running

```bash
uv run prism-backend --reload
uv run prism-backend
```

Direct backend runs default to port `8000`. When you launch through `../start.sh`, the root launcher exposes the backend on `http://localhost:18000` and wires the checked-in PostgreSQL compose helper plus frontend integration settings.

The API will be available at:
- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc
- **OpenAPI JSON**: http://localhost:8000/openapi.json

---

## Testing

```bash
uv run pytest tests/
uv run pytest tests/ --cov=app --cov-report=html
uv run pytest tests/test_smoke_defect_regressions.py -v
uv run pytest tests/services/test_loadbalancer_planner.py -v
```

### Test layout

- Most backend pytest runs require Docker because `tests/conftest.py` starts PostgreSQL through `testcontainers` and applies Alembic migrations before the session begins.
- The small DB-free allowlist currently includes `tests/services/test_background_tasks.py`, `tests/test_backend_version_metadata.py`, and `tests/test_realtime_broadcast.py`.
- `tests/test_smoke_defect_regressions.py` re-exports the named DEF smoke corpus, including grouped config, costing, startup, and proxy regressions.
- `tests/test_multi_profile_isolation.py` owns selected-profile versus active-runtime containment coverage.
- `tests/test_realtime_broadcast.py` owns websocket auth, subscribe/unsubscribe, and `dashboard.update` coverage.
- `tests/services/` keeps service-focused auth, loadbalancer, stats, streaming, throughput, and WebAuthn tests out of the smoke and isolation trees.

`pyproject.toml` is the dependency declaration source and `uv.lock` pins the resolved environment. Local development uses `uv sync --locked`, while production images install runtime dependencies with `uv sync --locked --no-dev`.

---

## Configuration

### Environment variables

- `PORT` - Server port for `prism-backend` (default: `8000`)
- `PRISM_BACKEND_WORKERS` - Worker count when `--reload` is off (default: `4`)
- `DATABASE_URL` - PostgreSQL DSN for direct backend runs

For direct runs, other common settings come from `../.env.example`, especially `HOST`, `CORS_ALLOWED_ORIGINS`, auth or WebAuthn settings, and SMTP settings for email verification or password reset flows.

### Database

Schema migrations are managed with Alembic and applied automatically on backend startup (`upgrade head`).

For local development, run PostgreSQL via Docker Compose:

```bash
docker compose up -d postgres
```

The checked-in compose file exposes PostgreSQL on `localhost:15432`. The root launcher uses that same compose file and wires `DATABASE_URL` accordingly. If you run `uv run prism-backend` directly against the checked-in compose file, point `DATABASE_URL` at `postgresql+asyncpg://prism:prism@localhost:15432/prism`.

---

## API overview

### Management API

- `GET /api/auth/*`, `GET /api/vendors`, `GET /api/models`, `GET /api/endpoints`, `GET /api/connections/*`
- `GET /api/profiles`, `GET /api/settings`, `GET /api/pricing-templates`
- `GET /api/stats/requests`, `GET /api/stats/summary`, `GET /api/stats/connection-success-rates`
- `GET /api/audit/logs`, `GET /api/audit/logs/{id}`
- `GET /api/loadbalance/current-state`, `GET /api/loadbalance/events`
- `GET /api/config/export`, `POST /api/config/import`
- `GET /health` and `/api/realtime/ws` round out the live backend surface for health checks and dashboard updates

### Proxy API

- `POST /v1/*` - OpenAI runtime proxy routes
- `POST /v1/messages*` - Anthropic runtime proxy routes
- `POST /v1beta/models/*` - Gemini native runtime proxy routes

Prism accepts API-family-native path families only: OpenAI models on `/v1/*`, Anthropic models on `/v1/messages`, and Gemini models on `/v1beta/models/*`.

---

## Key concepts

### Native vs proxy models

- **Native**: real models with their own connection configurations and load balancing
- **Proxy**: models that forward requests to native targets while keeping their own routing metadata

### Load balancing strategies

- **single**: always use the first active connection
- **round-robin**: rotate the primary attempt across active connections in order
- **failover**: try connections in priority order with adaptive auto-recovery
- **fill-first**: strict priority spillover after the active target is exhausted

### Audit logging

Optional per-vendor request/response body capture with automatic header redaction:
- `authorization`, `x-api-key`, `x-goog-api-key`, and headers matching `key|secret|token|auth`
- body capture can be disabled per vendor

---

## Development notes

- All database operations and HTTP requests use async/await.
- Streaming responses use a separate `AsyncSessionLocal()` in the generator's `finally` block.
- API-family-specific auth headers are built in `proxy_service.py`.
- Alembic revisions are the source of truth; create them with `uv run alembic revision --autogenerate -m "message"`.
