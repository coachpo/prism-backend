# BACKEND STATS SERVICE KNOWLEDGE BASE

## OVERVIEW
`services/stats/` is the telemetry and query cluster behind `../stats_service.py`: request-log writes, filtered request-log listing with request-id focus, summary rollups, throughput reports, spending reports, preset time-window resolution, and realtime request-log/statistics payload emission.

## STRUCTURE
```
stats/
├── __init__.py         # Re-export boundary consumed by stats_service.py
├── logging.py          # Request-log writes during proxy execution
├── request_logs.py     # Filtered request-log listing and pagination, including request-id focus
├── spending.py         # Spending report aggregation and top-N rollups
├── summary.py          # Summary cards, model health, success-rate queries
├── throughput.py       # Time-bucket throughput reports
├── time_presets.py     # Preset -> datetime window normalization
└── usage_extractors.py # Provider-specific token extraction helpers
```

## WHERE TO LOOK

- Re-export surface: `__init__.py`
- Proxy-side request logging: `logging.py`
- Operations/request-log table data and request focus lookups: `request_logs.py`
- Spending filters, grouping, and top spenders: `spending.py`
- Summary cards and connection/model success rates: `summary.py`
- Throughput bucket aggregation: `throughput.py`
- Time-window presets shared by summary and spending: `time_presets.py`
- Provider token parsing fallbacks: `usage_extractors.py`

## CONVENTIONS

- Keep this package query-focused; `../stats_service.py` is the public import surface, but the real logic lives here.
- All queries stay profile-scoped; never aggregate request logs across profiles.
- Reuse `resolve_time_preset()` instead of duplicating preset/date-window math in other modules.
- Treat null-vs-zero token and cost fields deliberately; missing usage should not silently become priced usage.
- Keep provider token parsing isolated to `usage_extractors.py`.
- Keep throughput aggregation in `throughput.py` rather than layering it into `summary.py` or route handlers.
- `logging.py` owns request-log side effects, including payload broadcasts and dirty-signal fallbacks; callers should not duplicate those websocket emissions.

## ANTI-PATTERNS

- Do not bypass this package by re-implementing summary or spending SQL inside routers.
- Do not mix successful and failed-request semantics; spending and report queries intentionally key off success and billable flags.
- Do not add provider-specific token parsing outside `usage_extractors.py`.
- Do not use request-scoped DB sessions during streaming finalization; `logging.py` opens its own `AsyncSessionLocal()` for that path.
