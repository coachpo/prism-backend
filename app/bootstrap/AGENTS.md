# BACKEND BOOTSTRAP KNOWLEDGE BASE

## OVERVIEW
`bootstrap/` owns startup sequencing and the shared auth middleware mounted by `app/main.py`; `main.py` wraps that startup sequence with lifespan-managed shared infrastructure setup and teardown.

## STRUCTURE
```
bootstrap/
├── startup.py          # Startup migrations, seed defaults, secret encryption, shared httpx client builder
└── auth_middleware.py  # /api vs /v1 auth bifurcation, public-management exceptions, auth error responses
```

## WHERE TO LOOK

- Startup order, provider seeds, profile invariants, default user settings, auth settings, and blocklist defaults: `startup.py`
- Public management exceptions and auth-path split: `auth_middleware.py`
- CORS-aware auth error responses: `auth_middleware.py`
- Main lifecycle wiring, shared `httpx.AsyncClient`, and `background_task_manager`: `../main.py`

## CONVENTIONS

- Keep startup ordering centralized in `run_startup_sequence()`; `main.py` should call it from lifespan, then manage shared client and worker startup and teardown around it.
- Keep `/api/*` cookie auth and `/v1*` plus `/v1beta*` proxy-key auth split in `auth_middleware.py`.
- Mirror allowed origins on auth error responses when the request `Origin` is explicitly allowed.
- Add new unauthenticated management routes to `PUBLIC_MANAGEMENT_PATHS` instead of hand-rolling route exceptions.

## ANTI-PATTERNS

- Do not reintroduce deprecated startup-event handlers when lifespan already owns bootstrap timing.
- Do not push session-cookie or proxy-key enforcement into routers; middleware already owns that contract.
- Do not bypass startup seeds for providers, profile invariants, auth settings, or system header blocklist rules.
