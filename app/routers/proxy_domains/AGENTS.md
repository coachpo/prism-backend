# BACKEND PROXY DOMAINS KNOWLEDGE BASE

## OVERVIEW
`proxy_domains/` is the dense runtime helper package behind `../proxy.py`. It turns an incoming `/v1/*` or `/v1beta/*` request into a prepared attempt plan, executes buffered or streaming upstream attempts, records failover or recovery outcomes, and finishes request-log plus audit side effects.

## STRUCTURE
```
proxy_domains/
├── __init__.py                    # Package marker
├── request_setup.py               # Model resolution, path validation, attempt planning, costing context
├── proxy_request_helpers.py       # Request-path helpers, model rewrites, detached-task tracking, failover classification helpers
├── attempt_types.py               # Shared dataclasses for runtime deps, request state, and attempt targets
├── attempt_execution.py           # Per-connection attempt loop and final 502 or 503 fallback
├── attempt_handlers.py            # Buffered and streaming attempt handling plus failure or recovery recording
├── attempt_streaming.py           # SSE passthrough, token extraction, and stream finalization flow
└── attempt_outcome_reporting.py   # Request-log, audit-log, and background finalization helpers
```

## WHERE TO LOOK

- Request setup, model rewrite, api-family path validation, requested-model vendor attribution, and load-balance attempt planning: `request_setup.py`, `proxy_request_helpers.py`
- Shared runtime dependency contracts and attempt target state: `attempt_types.py`
- Main per-connection execution loop, endpoint activity checks, and probe-eligible claims: `attempt_execution.py`
- Buffered or streaming upstream handling, failover classification, and recovery recording: `attempt_handlers.py`
- Streaming token extraction, SSE finalization, and inline fallback logging: `attempt_streaming.py`, `attempt_outcome_reporting.py`
- Upstream URL, header, and transport seams consumed from services: `../../services/proxy_support/AGENTS.md`, `../../services/proxy_service.py`
- Failover planner and recovery state touched from this package: `../../services/loadbalancer/AGENTS.md`

## PACKAGE FACTS

- `request_setup.py` resolves the routed model from body or Gemini-style path data, validates api-family-native path families, keeps requested-model vendor attribution separate from resolved-target runtime routing, rewrites model identifiers when a proxy model targets a different upstream model, and builds the costing context used later by request logging.
- `attempt_execution.py` owns the ordered connection loop. It skips disabled connections, claims probe-eligible recovery slots when needed, and returns the first successful buffered or streaming response.
- `attempt_handlers.py` decides when an upstream status should fail over versus return directly, and records connection failure or recovery state only when failover recovery is active for the model.
- `attempt_streaming.py` keeps streaming finalization separate from the request-scoped DB lifetime. It extracts token usage from SSE payloads when possible, then hands request-log and audit follow-up work to `background_task_manager` with an inline fallback path.
- `attempt_outcome_reporting.py` is the shared side-effect layer for both buffered and streaming attempts. It turns an attempt result into request-log writes, costing fields, and optional audit persistence.

## CONVENTIONS

- Keep `proxy.py` thin. New runtime proxy behavior should land in this package or in the service packages it already depends on.
- Keep request setup separate from attempt execution so model resolution, path rewrites, and costing setup stay testable without making upstream calls.
- Keep streaming-specific parsing and finalization inside `attempt_streaming.py` and `attempt_outcome_reporting.py` instead of mixing that flow into buffered handlers.
- Reuse the typed dependency seam in `attempt_types.py` when wiring new runtime collaborators.

## ANTI-PATTERNS

- Do not move runtime proxy business logic back into `proxy.py`.
- Do not treat management profile overrides as valid routing input here. This package runs on active-profile runtime semantics.
- Do not duplicate upstream URL, header, or transport code that already belongs in `services/proxy_support/` or `services/proxy_service.py`.
- Do not bypass the shared request-log and audit helpers when adding new buffered or streaming outcomes.
