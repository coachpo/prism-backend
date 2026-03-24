# BACKEND ROUTERS KNOWLEDGE BASE

## OVERVIEW
`routers/` is the backend API surface. `main.py` mounts 14 top-level routers here, and most management areas stay thin by handing dense request logic to existing `*_domains/` folders. The main exceptions are the small standalone routers such as `audit.py`, `loadbalance.py`, `providers.py`, and the websocket-focused `realtime.py`. `proxy_domains/` is now a documented runtime leaf because it has become its own dense proxy-execution cluster.

## STRUCTURE
```
routers/
├── auth.py + auth_domains/                       # Session, password reset, passkey, cookie auth flows
├── config.py + config_domains/                   # Config export or import and header blocklist flows
├── connections.py + connections_domains/         # Connection CRUD, pricing linkage, health checks, ordering
├── endpoints.py + endpoints_domains/             # Endpoint CRUD, duplication, dropdown and ordering flows
├── models.py + models_domains/                   # Model CRUD, proxy-model invariants, batch lookups
├── pricing_templates.py + pricing_templates_domains/ # Pricing template CRUD and usage lookups
├── profiles.py + profiles_domains/               # Profile lifecycle, activation CAS, soft delete
├── settings.py + settings_domains/               # Auth settings, costing, timezone, verification, proxy keys
├── stats.py + stats_domains/                     # Request logs, summary, throughput, metrics batch APIs
├── proxy.py + proxy_domains/                     # Runtime `/v1*` and `/v1beta*` proxy execution
├── shared/                                       # Router-layer helpers reused across management routes
├── audit.py                                      # Audit log queries and retention delete responses
├── loadbalance.py                                # Strategy CRUD plus current-state and loadbalance event management APIs
├── providers.py                                  # Provider audit-setting management
└── realtime.py                                   # Websocket auth and profile-channel subscription flow
```

## WHERE TO LOOK

- Auth and passkey route entrypoints: `auth.py`, `auth_domains/`
- Config import or export and blocklist routes: `config.py`, `config_domains/`
- Connection and endpoint CRUD flows: `connections.py`, `connections_domains/`, `endpoints.py`, `endpoints_domains/`
- Model, pricing template, and profile management: `models.py`, `models_domains/`, `pricing_templates.py`, `pricing_templates_domains/`, `profiles.py`, `profiles_domains/`
- Settings composition router and subdomains: `settings.py`, `settings_domains/`
- Stats request-log, throughput, summary, and metrics batch handlers: `stats.py`, `stats_domains/`
- Loadbalance strategy CRUD, current-state reads or resets, and event management: `loadbalance.py`
- Runtime proxy path handling, attempts, streaming, and outcome reporting: `proxy.py`, `proxy_domains/`, `proxy_domains/AGENTS.md`
- Websocket auth, subscribe or unsubscribe flow, and channel validation: `realtime.py`
- Shared room-state ownership behind realtime: `../services/realtime/connection_manager.py`

## ROUTER FACTS

- Route shells stay intentionally thin when a matching domain folder exists.
- `proxy.py` is the runtime entrypoint and is not an `/api` management router.
- `realtime.py` owns websocket authentication, supported-channel validation, profile existence checks, and subscribe or unsubscribe messages.
- `realtime.py` delegates connection tracking and room membership to `services/realtime/connection_manager.py`.
- Parent coverage still applies to `auth_domains/`, `config_domains/`, `connections_domains/`, `endpoints_domains/`, `models_domains/`, `pricing_templates_domains/`, `profiles_domains/`, `settings_domains/`, and `stats_domains/`. `proxy_domains/` now has its own leaf AGENTS doc because its runtime package surface is large enough to justify one.

## CONVENTIONS

- Put heavy request logic in the existing domain folders or services, not back into the shell routers.
- Use `dependencies.py` for effective-profile and active-profile resolution instead of ad hoc header parsing.
- Keep `config.py` and `settings.py` as composition routers that stitch existing domain modules together.
- Keep runtime proxy orchestration in `proxy_domains/` and use its leaf doc for that package's internal boundary map.
- Keep websocket room state out of routers. `realtime.py` should authenticate and route messages, then hand room state to the connection manager.

## ANTI-PATTERNS

- Do not move business logic from `*_domains/` back into `models.py`, `connections.py`, `settings.py`, or other shell routers.
- Do not treat `proxy.py` as if management profile overrides apply there. Runtime routing uses active-profile semantics.
- Do not invent new router-domain folders in docs that are not present under `routers/`.
- Do not duplicate child-level internals here when the parent map is enough.
