# BACKEND SERVICES ROOT KNOWLEDGE BASE

## OVERVIEW
`services/` is the backend service boundary: public facades such as `auth_service.py`, `proxy_service.py`, `stats_service.py`, and `webauthn_service.py`; shared infrastructure such as `background_tasks.py`; and split child packages for deeper domains.

## STRUCTURE
```
services/
├── AGENTS.md                           # Service-root map
├── auth/AGENTS.md                      # Session, email, password reset, proxy keys
├── realtime/AGENTS.md                  # WebSocket room state and broadcasts
├── stats/AGENTS.md                     # Telemetry queries and dashboard payload assembly
├── loadbalancer_support/AGENTS.md      # Recovery state, attempts, event helpers
├── proxy_support/AGENTS.md             # URL/header/body/transport helpers
├── webauthn/AGENTS.md                  # Passkey registration/authentication internals
├── auth_service.py                     # Auth public re-export boundary
├── webauthn_service.py                 # Passkey public re-export boundary
├── stats_service.py                    # Stats public re-export boundary
├── proxy_service.py                    # Proxy orchestration boundary
├── loadbalancer.py                     # Runtime model resolution and connection selection
├── audit_service.py                    # Audit persistence and redaction
├── costing_service.py                  # Pricing and FX helpers
├── background_tasks.py                 # Shared async worker queue
├── background_cleanup.py               # Request/audit retention cleanup helpers
├── loadbalance_cleanup.py              # Loadbalance-event retention cleanup helpers
├── profile_invariants.py               # Active/default profile enforcement
└── user_settings.py                    # Per-profile settings bootstrap and access helpers
```

## WHERE TO LOOK

- Shared worker lifecycle and retry semantics: `background_tasks.py`, `../main.py`
- Public auth/session boundary: `auth_service.py`, `auth/AGENTS.md`
- Runtime routing and upstream forwarding: `loadbalancer.py`, `proxy_service.py`, `loadbalancer_support/AGENTS.md`, `proxy_support/AGENTS.md`
- Observability and realtime payload shaping: `stats_service.py`, `audit_service.py`, `stats/AGENTS.md`, `realtime/AGENTS.md`
- Passkey public boundary: `webauthn_service.py`, `webauthn/AGENTS.md`
- Startup-enforced invariants and defaults: `profile_invariants.py`, `user_settings.py`
- Retention cleanup helpers: `background_cleanup.py`, `loadbalance_cleanup.py`

## CHILD DOCS

- `auth/AGENTS.md`: singleton auth settings, sessions, email delivery, password reset, and proxy keys.
- `loadbalancer_support/AGENTS.md`: recovery-state mutation, attempt planning, and loadbalance-event helpers.
- `proxy_support/AGENTS.md`: upstream URL/header/body/compression/transport helpers.
- `realtime/AGENTS.md`: websocket room state and broadcast fan-out.
- `stats/AGENTS.md`: request logging, aggregations, spending, throughput, and dashboard payload assembly.
- `webauthn/AGENTS.md`: passkey registration, authentication, and credential management.

## CONVENTIONS

- Treat `auth_service.py`, `stats_service.py`, and `webauthn_service.py` as public import surfaces over their split packages.
- Keep router handlers thin by pushing durable business logic into these service boundaries instead of re-implementing it in `routers/`.
- Keep shared worker lifecycle in `../main.py` plus `background_tasks.py`; the queue is app-owned infrastructure, not a per-request helper.
- Keep cleanup helpers separate from query/CRUD services so retention flows stay explicit and testable.
- Keep WebAuthn separate from the auth package; passkey browser/server ceremony is its own boundary.

## ANTI-PATTERNS

- Do not import leaf package internals directly when a service-root facade already exists.
- Do not spawn orphan asyncio workers from feature code when `background_tasks.py` already owns the shared queue.
- Do not move pricing, audit, or routing logic back into routers once a service boundary exists.
