# Prism Backend

**FastAPI-based proxy engine with async request routing, load balancing, and telemetry.**

This is the backend component of Prism, handling all LLM API routing, load balancing, health tracking, and data persistence.

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
│   │   ├── proxy_domains/           # Proxy setup, attempts, streaming, and logging helpers
│   │   ├── shared/                  # Router-layer shared helpers for profile rows and ordering
│   │   ├── stats_domains/           # Request log, metrics, spending, and throughput handlers
│   │   ├── providers.py             # Provider CRUD
│   │   ├── models.py                # Thin model route shell
│   │   ├── endpoints.py             # Thin endpoint route shell
│   │   ├── connections.py           # Thin connection route shell
│   │   ├── stats.py                 # Thin stats route shell
│   │   ├── audit.py                 # Audit log queries
│   │   ├── config.py                # Thin config route shell
│   │   ├── pricing_templates.py     # Thin pricing template route shell
│   │   ├── profiles.py              # Thin profile route shell
│   │   └── proxy.py                 # Thin /v1/* and /v1beta/* proxy router
│   └── services/
│       ├── auth/                    # Split auth, email, session, and proxy-key services
│       ├── loadbalancer_support/    # Recovery state, attempts, event helpers
│       ├── proxy_support/           # URL/header/body/transport helpers
│       ├── realtime/                # WebSocket connection manager helpers
│       ├── stats/                   # Telemetry query and logging helpers
│       ├── webauthn/                # Passkey registration/authentication internals
│       ├── auth_service.py          # Auth public re-export boundary
│       ├── background_tasks.py      # Lifespan-managed async worker queue
│       ├── loadbalancer.py          # Model resolution + connection selection
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
├── pyproject.toml                   # Runtime deps, dev extras, and console script
├── tests/                           # Pytest test suite
└── AGENTS.md                        # Backend knowledge base
```

---

## Setup

### Prerequisites
- Python 3.13+
- pip

### Installation

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install runtime + dev dependencies
pip install -e ".[dev]"
```

### Running

```bash
# Development server with auto-reload
prism-backend --reload

# Production defaults (4 workers, proxy headers enabled)
prism-backend
```

The API will be available at:
- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc
- **OpenAPI JSON**: http://localhost:8000/openapi.json

---

## Testing

```bash
# Run all tests
pytest tests/

# Run with coverage
pytest tests/ --cov=app --cov-report=html

# Run specific test file
pytest tests/test_proxy.py -v
```

`pyproject.toml` is the single dependency source. Runtime installs use `pip install .`,
and local dev/test installs use `pip install -e ".[dev]"` so pytest tooling stays out
of production images.

---

## Configuration

### Environment Variables

- `PORT` - Server port for `prism-backend` (default: `8000`)
- `PRISM_BACKEND_WORKERS` - Worker count when `--reload` is off (default: `4`)
- `DATABASE_URL` - PostgreSQL DSN (default: `postgresql+asyncpg://prism:prism@localhost:5432/prism`)

### Database

Schema migrations are managed with Alembic and applied automatically on backend startup (`upgrade head`).

For local development, run PostgreSQL via Docker Compose:

```bash
docker compose up -d postgres
```

---

## API Overview

### Management API

- `GET /api/providers` - List all providers
- `GET /api/providers/{id}` - Get a single provider
- `PATCH /api/providers/{id}` - Update provider audit settings

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

- `GET /api/config/export` - Export full configuration
- `POST /api/config/import` - Import configuration

### Proxy API

- `POST /v1/*` - OpenAI runtime proxy routes
- `POST /v1/messages*` - Anthropic runtime proxy routes
- `POST /v1beta/models/*` - Gemini native runtime proxy routes

Prism accepts provider-native path families only: OpenAI models on `/v1/*`, Anthropic models on `/v1/messages`, and Gemini models on `/v1beta/models/*`.

---

## Key Concepts

### Native vs Proxy Models

- **Native**: Real models with their own connection configurations and load balancing
- **Proxy**: Alias models that forward to a native model (for ID resolution) — no connections of their own

### Load Balancing Strategies

- **single**: Always use the first active connection (priority 0)
- **failover**: Try connections in priority order with adaptive auto-recovery (failure threshold, exponential backoff, jitter, and auth-like cooldown handling)

### Success Rate Tracking

Connections display a success rate badge computed from `request_logs` data (last 24h):
- ≥98% = green
- 75-98% = yellow
- <75% = red
- No data = gray (N/A)

### Audit Logging

Optional per-provider request/response body capture with automatic header redaction:
- Redacted headers: `authorization`, `x-api-key`, `x-goog-api-key`, and any header matching `key|secret|token|auth` pattern
- Body capture can be disabled per-provider

---

## Development Notes

### Async Everywhere

All database operations and HTTP requests use async/await. Never use blocking I/O.

### Streaming Logs

Streaming responses use a separate `AsyncSessionLocal()` in the generator's `finally` block because the request-scoped session is closed before streaming completes.

### Provider Auth Headers

Provider-specific auth headers are built in `proxy_service.py`:
- OpenAI: `Authorization: Bearer {api_key}`
- Anthropic: `x-api-key: {api_key}`
- Gemini: `Authorization: Bearer {api_key}`

### Schema Migrations

Alembic migrations are the source of truth. Create revisions with `alembic revision --autogenerate -m "message"` and apply with `alembic upgrade head`.

---

## Troubleshooting

### Database Connection Errors

If startup fails to connect to PostgreSQL, verify your `DATABASE_URL` and ensure the Postgres service is healthy (`docker compose ps`).

### Import Errors

Make sure you're running from the `backend/` directory and the virtual environment is activated.

### Port Already in Use

Change the port with `--port` or set the `PORT` environment variable.

---

## Contributing

This repo does not currently include a shared `CONTRIBUTING.md`; follow the backend
module conventions in `AGENTS.md` and the surrounding code.

---

## License

This repo does not currently include a standalone `LICENSE` file.
