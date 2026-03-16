# BACKEND ROUTERS KNOWLEDGE BASE

## OVERVIEW
`routers/` is the backend API surface: 14 top-level FastAPI router shells plus domain folders that keep heavy request logic out of the route entrypoints.

## STRUCTURE
```
routers/
├── auth.py + auth_domains/               # Session, password reset, WebAuthn, cookie helpers
├── config.py + config_domains/           # Config export/import and header blocklist flows
├── connections.py + connections_domains/ # CRUD, pricing, health checks, owner lookups, reordering
├── endpoints.py + endpoints_domains/     # Endpoint CRUD, duplication, dropdown data
├── models.py + models_domains/           # Model query and mutation split
├── pricing_templates.py + pricing_templates_domains/ # Pricing template CRUD and usage lookups
├── profiles.py + profiles_domains/       # Profile CRUD, activation CAS, soft delete
├── proxy.py + proxy_domains/             # /v1* and /v1beta* runtime proxy execution
├── shared/                               # Reused router-layer helpers for profile rows and ordering
├── settings.py + settings_domains/       # Auth settings, costing, timezone, email verification, proxy keys
├── stats.py + stats_domains/             # Request logs, metrics batch, summary, spending, throughput
├── audit.py                              # Audit log queries and retention deletes
├── loadbalance.py                        # Persistent loadbalance event queries and stats
├── providers.py                          # Provider audit settings
└── realtime.py                           # WebSocket subscribe/auth/stats endpoint
```

## WHERE TO LOOK

- Auth/session/passkey handlers: `auth.py`, `auth_domains/`
- Config v2 import/export and blocklist rules: `config.py`, `config_domains/`
- Connection CRUD, pricing-template assignment, owner lookup, health checks: `connections.py`, `connections_domains/`
- Endpoint CRUD, duplication, and ordering: `endpoints.py`, `endpoints_domains/`
- Model queries, proxy-model invariants, redirect validation: `models.py`, `models_domains/`
- Pricing template CRUD and usage lookups: `pricing_templates.py`, `pricing_templates_domains/`
- Profile lifecycle and active-profile CAS: `profiles.py`, `profiles_domains/`
- Runtime proxy attempt setup, streaming, logging, and failover handlers: `proxy.py`, `proxy_domains/`
- Shared router-layer helpers: `shared/`
- Settings subrouters for auth, costing, timezone, email verification, and proxy keys: `settings.py`, `settings_domains/`
- Observability APIs: `stats.py`, `stats_domains/request_logs_route_handlers.py`, `audit.py`, `loadbalance.py`, `realtime.py`
- Metrics batching: `stats_domains/metrics_route_handlers.py` for model and connection metrics.

## CONVENTIONS

- Keep top-level router files thin; if a matching `*_domains/` folder exists, put request logic there instead of in the shell file.
- Reuse `shared/` helpers for profile-row locking, endpoint-record invariants, and ordered-field normalization instead of re-implementing them per domain.
- Profile-scoped management routes use `get_effective_profile*` dependencies; global management routes use plain DB/session auth dependencies; runtime proxy routes use `get_active_profile*` dependencies.
- `settings.py` and `config.py` are composition routers; extend the domain folders instead of bloating the parent shell.
- `proxy_domains/` owns attempt setup, streaming/buffered response handling, logging, and helper types; `proxy.py` should stay close to wiring.
- `realtime.py` owns websocket auth and subscribe/unsubscribe flow, while room state lives in `services/realtime/connection_manager.py`.

## ANTI-PATTERNS

- Do not parse `X-Profile-Id` ad hoc inside router handlers when `dependencies.py` already owns that contract.
- Do not push business logic back into `models.py`, `connections.py`, `settings.py`, or other shell routers when a domain module already exists.
- Do not treat `proxy.py` as a management router; it owns `/v1*` and `/v1beta*` runtime paths with active-profile semantics.
- Do not bypass domain helpers for header blocklists, proxy-model validation, or provider-specific health checks.
