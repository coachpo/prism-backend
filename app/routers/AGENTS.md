# BACKEND ROUTERS KNOWLEDGE BASE

## OVERVIEW
`routers/` is the backend API surface. `main.py` mounts the top-level routers here, and the management surface stays thin by handing dense request logic to documented `*_domains/` packages. The main parent-covered exceptions are the standalone routers `audit.py`, `loadbalance.py`, `monitoring.py`, `vendors.py`, and `realtime.py`, while reusable router helpers now have their own `shared/AGENTS.md` leaf.

## STRUCTURE
```
routers/
├── auth.py + auth_domains/AGENTS.md                     # Session, password reset, passkey, cookie auth flows
├── config.py + config_domains/AGENTS.md                 # Config export/import and header blocklist flows
├── connections.py + connections_domains/AGENTS.md       # Connection CRUD, health checks, ordering, owner helpers
├── endpoints.py + endpoints_domains/AGENTS.md           # Endpoint CRUD, duplication, dropdown, and ordering flows
├── models.py + models_domains/AGENTS.md                 # Model CRUD, proxy-model invariants, batch lookups
├── pricing_templates.py + pricing_templates_domains/AGENTS.md
├── profiles.py + profiles_domains/AGENTS.md             # Profile lifecycle, activation, and invariants handoff
├── settings.py + settings_domains/AGENTS.md             # Auth settings, costing, timezone, email verification, proxy keys
├── stats.py + stats_domains/AGENTS.md                   # Request logs, summary, throughput, spending, metrics batch APIs
├── monitoring.py                                        # Monitoring overview, vendor, model, and manual-probe APIs
├── proxy.py + proxy_domains/AGENTS.md                   # Runtime `/v1*` and `/v1beta*` proxy execution
├── shared/AGENTS.md                                     # Router-layer ordering, endpoint-record, and profile-row helpers
├── audit.py                                             # Audit log queries and retention delete responses
├── loadbalance.py                                       # Strategy CRUD plus current-state and event management APIs
├── vendors.py                                           # Global vendor CRUD and audit-setting management
└── realtime.py                                          # Websocket auth and profile-channel subscription flow
```

## CHILD DOCS

- `auth_domains/AGENTS.md`: session bootstrap, cookie helpers, password reset, and WebAuthn route handlers.
- `config_domains/AGENTS.md`: import/export pipeline and header blocklist CRUD.
- `connections_domains/AGENTS.md`: dense connection-management package, including the nested CRUD handler cluster.
- `endpoints_domains/AGENTS.md`, `models_domains/AGENTS.md`, `pricing_templates_domains/AGENTS.md`, `profiles_domains/AGENTS.md`: management CRUD/query leaves.
- `settings_domains/AGENTS.md`: auth-settings, costing/timezone, email-verification, and proxy-key route handlers.
- `stats_domains/AGENTS.md`: request-log, summary, throughput, spending, and metrics batch helpers.
- `monitoring.py`: monitoring overview, vendor drill-down, model detail, and manual-probe routes. Its dense logic lives behind `../services/monitoring_service.py`.
- `proxy_domains/AGENTS.md`: runtime proxy setup, attempts, streaming, and reporting.
- `shared/AGENTS.md`: reusable router-layer ordering, endpoint-record, and profile-row helpers.
- `loadbalance.py`: strategy CRUD plus current-state and event management. Its internals are covered through `../services/loadbalancer/AGENTS.md`.

## WHERE TO LOOK

- Auth and passkey route entrypoints: `auth.py`, `auth_domains/AGENTS.md`
- Config import/export and blocklist routes: `config.py`, `config_domains/AGENTS.md`
- Connection and endpoint CRUD flows: `connections.py`, `connections_domains/AGENTS.md`, `endpoints.py`, `endpoints_domains/AGENTS.md`
- Model, pricing template, and profile management: `models.py`, `models_domains/AGENTS.md`, `pricing_templates.py`, `pricing_templates_domains/AGENTS.md`, `profiles.py`, `profiles_domains/AGENTS.md`
- Settings composition router and subdomains: `settings.py`, `settings_domains/AGENTS.md`
- Stats request-log, throughput, summary, spending, and metrics batch handlers: `stats.py`, `stats_domains/AGENTS.md`
- Monitoring overview, vendor drill-down, model detail, and manual probes: `monitoring.py`, `../services/monitoring_service.py`
- Loadbalance strategy CRUD, current-state reads or resets, and event management: `loadbalance.py`, `../services/loadbalancer/AGENTS.md`
- Runtime proxy path handling, attempts, streaming, and outcome reporting: `proxy.py`, `proxy_domains/AGENTS.md`
- Shared router helpers reused across management routes: `shared/AGENTS.md`, `shared/endpoint_records.py`, `shared/ordering.py`, `shared/profile_rows.py`
- Websocket auth, subscribe/unsubscribe flow, and channel validation: `realtime.py`
- Shared room-state ownership behind realtime: `../services/realtime/connection_manager.py`

## ROUTER FACTS

- Route shells stay intentionally thin when a matching domain package exists.
- `proxy.py` is the runtime entrypoint and is not an `/api` management router.
- `monitoring.py` is a standalone management router that stays thin by delegating monitoring queries and manual probes to the monitoring service facade.
- `realtime.py` owns websocket authentication, supported-channel validation, profile existence checks, and subscribe/unsubscribe messages.
- `realtime.py` delegates connection tracking and room membership to `services/realtime/connection_manager.py`.
- The management `*_domains/` folders now have leaf AGENTS docs. Parent coverage mainly remains for the standalone routers.

## CONVENTIONS

- Put heavy request logic in the existing domain folders or services, not back into the shell routers.
- Use `dependencies.py` for effective-profile and active-profile resolution instead of ad hoc header parsing.
- Keep `config.py` and `settings.py` as composition routers that stitch existing domain modules together.
- Keep runtime proxy orchestration in `proxy_domains/` and use its leaf doc for that package's internal boundary map.
- Keep reusable ordering, endpoint-record, and profile-row helpers in `shared/` instead of scattering them across domain packages.
- Keep loadbalance orchestration in `../services/loadbalancer/AGENTS.md` and let `loadbalance.py` stay a thin router shell.
- Keep websocket room state out of routers. `realtime.py` should authenticate and route messages, then hand room state to the connection manager.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Prefer the best current implementation shape over preserving the old one. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS

- Do not move business logic from `*_domains/` back into `models.py`, `connections.py`, `settings.py`, or other shell routers.
- Do not duplicate reusable router-layer helpers inside multiple domain packages when `shared/` already owns them.
- Do not treat `proxy.py` as if management profile overrides apply there. Runtime routing uses active-profile semantics.
- Do not invent new router-domain folders in docs that are not present under `routers/`.
- Do not stale-claim that router-domain packages are parent-covered when a leaf AGENTS file now exists.
