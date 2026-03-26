# BACKEND PROXY SUPPORT KNOWLEDGE BASE

## OVERVIEW
`services/proxy_support/` is the split package behind `../proxy_service.py`: model extraction, stream detection, URL normalization, header sanitization, compression handling, buffered proxying, streaming proxying, and failover classification.

## STRUCTURE
```
proxy_support/
├── body.py         # Model extraction and stream-flag helpers
├── compression.py  # Compression request/response helpers
├── constants.py    # API-family auth, failover codes, hop-by-hop header constants
├── headers.py      # Upstream header assembly, sanitization, blocklist checks
├── transport.py    # Buffered proxy, streaming proxy, failover decisions
└── urls.py         # Base URL normalization, validation, upstream path building
```

## WHERE TO LOOK

- Public re-export boundary: `../proxy_service.py`, `__init__.py`
- Request-body model and stream parsing: `body.py`
- Compression request/response rules: `compression.py`
- API-family/header constants: `constants.py`
- Custom-header merge, auth-header injection, blocklist enforcement: `headers.py`
- Buffered + streaming transport and failover classification: `transport.py`
- Base URL validation and upstream URL building: `urls.py`

## CONVENTIONS

- Treat `../proxy_service.py` as the public import surface; keep leaf-module imports local to proxy internals.
- Normalize and validate endpoint base URLs through `urls.py` before persisting or proxying.
- Build upstream headers through `headers.py`; blocklist enforcement happens after custom-header merge.
- Keep stream/body parsing in `body.py` and compression semantics in `compression.py`.
- Keep failover classification in `transport.py` so routers and attempt handlers consume one rule set.

## ANTI-PATTERNS

- Do not bypass header sanitization or rebuild api-family auth headers in unrelated modules.
- Do not duplicate base-URL normalization or upstream-path assembly outside `urls.py`.
- Do not classify failover-worthy responses in routers when `transport.py` already owns that decision.
- Do not spread request-body model extraction across proxy handlers; use `body.py` helpers.
