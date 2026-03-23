# BACKEND SERVICES ROOT KNOWLEDGE BASE

## OVERVIEW
`services/` is the backend service boundary. It holds the public facades imported by routers, shared runtime infrastructure such as `background_tasks.py`, and split child packages for auth, proxy support, realtime, stats, load-balancing support, and WebAuthn.

## STRUCTURE
```
services/
├── auth_service.py                     # Public auth and proxy-key re-export surface
├── webauthn_service.py                 # Public passkey re-export surface
├── stats_service.py                    # Public stats and observability re-export surface
├── proxy_service.py                    # Upstream forwarding boundary
├── loadbalancer.py                     # Runtime model resolution and connection selection
├── audit_service.py                    # Audit persistence and redaction
├── costing_service.py                  # Pricing and FX helpers
├── background_tasks.py                 # Shared BackgroundTaskManager implementation
├── background_cleanup.py               # Request and audit retention cleanup helpers
├── loadbalance_cleanup.py              # Loadbalance-event retention cleanup helpers
├── profile_invariants.py               # Active or default profile enforcement
├── user_settings.py                    # Per-profile settings bootstrap and access helpers
├── auth/AGENTS.md                      # Session, email, password reset, proxy-key internals
├── loadbalancer_support/AGENTS.md      # Recovery state, attempts, event helpers
├── proxy_support/AGENTS.md             # Upstream URL, header, body, transport helpers
├── realtime/AGENTS.md                  # Connection manager room state and broadcasts
├── stats/AGENTS.md                     # Telemetry, spending, throughput, dashboard helpers
└── webauthn/AGENTS.md                  # Passkey registration, authentication, credential helpers
```

## WHERE TO LOOK

- Shared worker lifecycle and metrics snapshots: `background_tasks.py`, `../main.py`
- Public auth boundary: `auth_service.py`, `auth/AGENTS.md`
- Public passkey boundary: `webauthn_service.py`, `webauthn/AGENTS.md`
- Runtime routing, attempt planning, and upstream forwarding: `loadbalancer.py`, `proxy_service.py`, `loadbalancer_support/AGENTS.md`, `proxy_support/AGENTS.md`
- Observability, request logging, and dashboard payload shaping: `stats_service.py`, `audit_service.py`, `stats/AGENTS.md`
- Realtime room-state ownership: `realtime/AGENTS.md`, `realtime/connection_manager.py`
- Startup-enforced defaults and retention cleanup: `profile_invariants.py`, `user_settings.py`, `background_cleanup.py`, `loadbalance_cleanup.py`

## SERVICE FACTS

- `background_tasks.py` defines `BackgroundTaskManager`, queue and worker lifecycle, retry handling, enqueue rejection tracking, and metrics snapshots.
- FastAPI lifespan in `../main.py` configures `background_task_manager` with the settings-derived worker count, starts it, stores it on `app.state`, and shuts it down during teardown.
- `auth_service.py`, `stats_service.py`, and `webauthn_service.py` are intended public import surfaces over deeper packages.
- Realtime route handlers depend on `services/realtime/connection_manager.py` for connection tracking and room membership instead of owning that state themselves.

## CONVENTIONS

- Keep routers thin by importing service-root facades or package-owned helpers instead of duplicating durable business logic.
- Treat the shared background task manager as app-owned infrastructure. Start and stop it in lifespan, then consume it from feature code.
- Keep passkey logic separate from the auth package. The public boundary is `webauthn_service.py` plus `services/webauthn/`.
- Keep cleanup helpers explicit and separate from request-serving code so retention work stays testable.

## ANTI-PATTERNS

- Do not import deep package internals when a service-root facade already exists.
- Do not spawn ad hoc worker pools or background queues from feature code when `background_tasks.py` already owns the shared worker model.
- Do not push routing, auth, or observability logic back into route handlers once an established service boundary already owns it.
