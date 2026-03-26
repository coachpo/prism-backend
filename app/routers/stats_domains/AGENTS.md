# BACKEND STATS DOMAINS KNOWLEDGE BASE

## OVERVIEW
`stats_domains/` is the route-helper package behind `../stats.py`. It owns request-log listing, operations views, summary, throughput, spending, metric-batch handlers, and request-log deletion orchestration over the deeper stats service layer.

## STRUCTURE
```
stats_domains/
├── request_logs_route_handlers.py # Request-log list/operations/delete routes
├── summary_route_handlers.py      # Summary and connection-success-rate routes
├── metrics_route_handlers.py      # Model and connection metrics batch routes
├── spending_route_handlers.py     # Spending report route helpers
├── throughput_route_handlers.py   # Throughput report route helpers
└── helpers.py                     # Datetime normalization and numeric coercion helpers
```

## WHERE TO LOOK

- Request-log list, operations view, and delete orchestration: `request_logs_route_handlers.py`
- Summary and connection-success-rate helpers: `summary_route_handlers.py`
- Model and connection metrics batch helpers: `metrics_route_handlers.py`
- Spending report helpers: `spending_route_handlers.py`
- Throughput report helpers: `throughput_route_handlers.py`
- Shared datetime normalization and numeric coercion: `helpers.py`

## CONVENTIONS

- Keep `stats.py` thin and push request parsing plus response shaping into this package.
- Normalize datetime filters through `helpers.py` before calling the stats service layer.
- Keep request-log deletion wired through the background cleanup helper instead of inlining delete work in the route shell.
- Keep operations, summary, throughput, spending, and metrics batch handlers split instead of merging them into one large module.

## ANTI-PATTERNS

- Do not duplicate datetime normalization or numeric coercion in `stats.py`.
- Do not bypass the background delete helper for request-log deletion flows.
- Do not fold operations, spending, summary, and throughput logic into one catch-all handler module.
