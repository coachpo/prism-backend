# BACKEND STATS SERVICE KNOWLEDGE BASE

## OVERVIEW
`services/stats/` is the telemetry/query cluster behind `services/stats_service.py`: request-log writes, token extraction, summary rollups, spending reports, and preset time-window resolution.

## STRUCTURE
```
stats/
├── __init__.py         # Re-export boundary consumed by stats_service.py
├── logging.py          # Request-log writes during proxy execution
├── request_logs.py     # Filtered request-log listing and pagination
├── spending.py         # Spending report aggregation and top-N rollups
├── summary.py          # Summary cards, model health, success-rate queries
├── time_presets.py     # Preset -> datetime window normalization
└── usage_extractors.py # Provider-specific token extraction helpers
```

## WHERE TO LOOK

- Re-export surface: `__init__.py`
- Proxy-side request logging: `logging.py`
- Operations/request-log table data: `request_logs.py`
- Spending filters, grouping, and top spenders: `spending.py`
- Summary cards and connection/model success rates: `summary.py`
- Time-window presets shared by summary + spending: `time_presets.py`
- Provider token parsing fallbacks: `usage_extractors.py`

## CONVENTIONS

- Keep this package query-focused; `services/stats_service.py` is the public import surface, but the real logic lives here.
- All queries stay profile-scoped; never aggregate request logs across profiles.
- Reuse `resolve_time_preset()` instead of duplicating preset/date-window math in other modules.
- Treat null-vs-zero token/cost fields deliberately; missing usage should not silently become priced usage.

## ANTI-PATTERNS

- Do not bypass this package by re-implementing summary/spending SQL inside routers.
- Do not mix successful and failed-request semantics; spending/report queries intentionally key off success and billable flags.
- Do not add provider-specific token parsing outside `usage_extractors.py`.
