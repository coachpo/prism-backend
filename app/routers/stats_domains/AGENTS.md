# BACKEND STATS DOMAINS KNOWLEDGE BASE

## OVERVIEW
`stats_domains/` is the route-helper package behind `../stats.py`. It owns the unified usage-snapshot route, request-log listing, summary, throughput, spending, metric-batch handlers, and request-log deletion orchestration over the deeper stats service layer.

## STRUCTURE
```
stats_domains/
├── request_logs_route_handlers.py # Request-log list/delete routes
├── usage_snapshot_route_handlers.py # Unified usage-snapshot route
├── summary_route_handlers.py      # Summary and connection-success-rate routes
├── metrics_route_handlers.py      # Model and connection metrics batch routes
├── spending_route_handlers.py     # Spending report route helpers
├── throughput_route_handlers.py   # Throughput report route helpers
└── helpers.py                     # Datetime normalization and numeric coercion helpers
```

## WHERE TO LOOK

- Unified usage-snapshot route: `usage_snapshot_route_handlers.py`
- Request-log list and delete orchestration: `request_logs_route_handlers.py`
- Summary and connection-success-rate helpers: `summary_route_handlers.py`
- Model and connection metrics batch helpers: `metrics_route_handlers.py`
- Spending report helpers: `spending_route_handlers.py`
- Throughput report helpers: `throughput_route_handlers.py`
- Shared datetime normalization and numeric coercion: `helpers.py`

## CONVENTIONS

- Keep `stats.py` thin and push request parsing plus response shaping into this package.
- Normalize datetime filters through `helpers.py` before calling the stats service layer.
- Keep request-log deletion wired through the background cleanup helper instead of inlining delete work in the route shell.
- Keep usage-snapshot, summary, throughput, spending, and metrics batch handlers split instead of merging them into one large module.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Prefer the best current implementation shape over preserving the old one. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS

- Do not duplicate datetime normalization or numeric coercion in `stats.py`.
- Do not bypass the background delete helper for request-log deletion flows.
- Do not fold usage-snapshot, spending, summary, and throughput logic into one catch-all handler module.
