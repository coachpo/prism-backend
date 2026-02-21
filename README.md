# Prism Backend

**FastAPI-based proxy engine with async request routing, load balancing, and telemetry.**

This is the backend component of Prism, handling all LLM API routing, load balancing, health tracking, and data persistence.

---

## Architecture

- **Framework**: FastAPI with async/await throughout
- **Database**: SQLite with async SQLAlchemy (aiosqlite)
- **HTTP Client**: httpx.AsyncClient for upstream requests
- **Streaming**: SSE pass-through with async generators

---

## Project Structure

```
backend/
├── app/
│   ├── main.py                      # FastAPI app + startup/shutdown + schema migrations
│   ├── database.py                  # SQLAlchemy async engine + session factory
│   ├── models/
│   │   └── models.py                # ORM models (Provider, ModelConfig, Endpoint, etc.)
│   ├── routers/
│   │   ├── providers.py             # Provider CRUD
│   │   ├── models.py                # Model CRUD
│   │   ├── endpoints.py             # Endpoint CRUD + health checks
│   │   ├── stats.py                 # Request logs + aggregated statistics
│   │   ├── audit.py                 # Audit log queries
│   │   ├── config.py                # Config export/import
│   │   └── proxy.py                 # Catch-all /v1/* proxy router
│   └── services/
│       ├── loadbalancer.py          # Model resolution + endpoint selection
│       ├── proxy_service.py         # Upstream request forwarding
│       └── audit_service.py         # Audit log writing with header redaction
├── tests/                           # Pytest test suite
├── requirements.txt                 # Python dependencies
├── gateway.db                       # SQLite database (auto-created)
└── AGENTS.md                        # Backend knowledge base
```

---

## Setup

### Prerequisites
- Python 3.11+
- pip

### Installation

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Running

```bash
# Development server with auto-reload
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Production (no reload)
uvicorn app.main:app --host 0.0.0.0 --port 8000
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

---

## Configuration

### Environment Variables

- `BACKEND_PORT` - Server port (default: 8000)
- `DATABASE_URL` - SQLite database path (default: `gateway.db`)

### Database

The SQLite database is created automatically on first run. Schema migrations are handled manually via `_add_missing_columns()` in `main.py`.

**Database files:**
- `gateway.db` - Main database
- `gateway_smoke.db` - Separate database for smoke testing

---

## API Overview

### Management API

- `GET /providers` - List all providers
- `POST /providers` - Create provider
- `PUT /providers/{id}` - Update provider
- `DELETE /providers/{id}` - Delete provider

- `GET /models` - List all models
- `POST /models` - Create model
- `PUT /models/{id}` - Update model
- `DELETE /models/{id}` - Delete model

- `GET /endpoints` - List all endpoints
- `POST /endpoints` - Create endpoint
- `PUT /endpoints/{id}` - Update endpoint
- `DELETE /endpoints/{id}` - Delete endpoint
- `POST /endpoints/{id}/health` - Manual health check

- `GET /stats/requests` - Request logs with filters
- `GET /stats/summary` - Aggregated statistics
- `GET /stats/endpoint-success-rates` - Per-endpoint success rates

- `GET /audit/logs` - Audit logs with filters
- `GET /audit/logs/{id}` - Audit log detail

- `GET /config/export` - Export full configuration
- `POST /config/import` - Import configuration

### Proxy API

- `POST /v1/*` - Catch-all proxy endpoint (OpenAI-compatible)

All `/v1/*` requests are forwarded to the appropriate upstream provider based on the `model` field in the request body.

---

## Key Concepts

### Native vs Proxy Models

- **Native**: Real models with their own endpoint configurations and load balancing
- **Proxy**: Alias models that forward to a native model (for ID resolution)

### Load Balancing Strategies

- **single**: Always use the first active endpoint (priority 0)
- **round_robin**: Rotate through active endpoints
- **failover**: Try endpoints in priority order until one succeeds

### Success Rate Tracking

Endpoints display a success rate badge computed from `request_logs` data (last 24h):
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
- Gemini: `x-goog-api-key: {api_key}`

### Schema Migrations

Manual migrations via `_add_missing_columns()` in `main.py`. Add new columns with `ALTER TABLE` statements and handle `OperationalError` for existing columns.

---

## Troubleshooting

### Database Locked

If you see "database is locked" errors, ensure only one process is accessing the database at a time. SQLite doesn't support high concurrency.

### Import Errors

Make sure you're running from the `backend/` directory and the virtual environment is activated.

### Port Already in Use

Change the port with `--port` flag or set `BACKEND_PORT` environment variable.

---

## Contributing

See the main [CONTRIBUTING.md](../CONTRIBUTING.md) for guidelines.

---

## License

MIT License - see [LICENSE](../LICENSE) for details.
