# BACKEND MODELS KNOWLEDGE BASE

## OVERVIEW
ORM models for Prism, split into identity, routing, and observability domains. `models.py` is the supported re-export boundary that routers, services, and migrations should read from when they need the full ORM surface.

## STRUCTURE
```
models/
├── domains/
│   ├── identity.py       # Profiles, vendors, operator auth, password reset, refresh tokens, proxy keys, WebAuthn
│   ├── routing.py        # Model configs, endpoints, pricing templates, connections, api_family compatibility
│   └── observability.py  # Request logs, user settings, FX rates, blocklist rules, audit logs, loadbalance current state, loadbalance events
└── models.py             # Re-export boundary for all domain models
```

## DOMAINS

### IDENTITY
- `Profile`: Multi-profile scope container; owns `is_active` and `is_default` flags.
- `Vendor`: Global CRUD-managed publisher metadata, shared across profiles, with vendor keys, optional persisted `icon_key`, and display data.
- `AppAuthSettings`: Singleton operator auth state, username/email, and token version.
- `PasswordResetChallenge`: One-time password reset challenge rows, expiry, consumption, and attempt tracking.
- `RefreshToken`: Session persistence with rotation and revocation.
- `ProxyApiKey`: Runtime data-plane credentials with prefix/hash storage.
- `WebAuthnChallenge` & `WebAuthnCredential`: Passkey lifecycle and device metadata.

### ROUTING
- `ModelConfig`: Per-profile model settings that require both `vendor_id` and fixed `api_family`, plus attached loadbalance strategy selection for native models and ordered proxy-target routing for proxy models. Model rows do not carry vendor icon metadata.
- `ModelProxyTarget`: Ordered proxy-model target rows that connect one proxy model to one native target model.
- `LoadbalanceStrategy`: Profile-scoped reusable routing strategy rows for native model attachment, with `strategy_type = legacy | adaptive`, legacy `legacy_strategy_type + auto_recovery`, and adaptive `routing_policy` payloads.
- `Endpoint`: Encrypted upstream base URLs and API keys.
- `PricingTemplate`: Costing rules for input, output, reasoning, and cache tokens.
- `Connection`: Linkage between model configs, endpoints, and pricing; owns health status.

### OBSERVABILITY
- `RequestLog`: High-volume telemetry for all proxy traffic; owns requested model identity, resolved target identity, and `api_family` filters, along with billable and priced flags.
- `UserSetting`: Per-profile currency and timezone preferences.
- `EndpointFxRateSetting`: Custom FX rates for specific model/endpoint pairs.
- `HeaderBlocklistRule`: System and profile-scoped rules for stripping sensitive headers.
- `AuditLog`: Detailed request/response capture for audited vendors.
- `LoadbalanceCurrentState`: Persisted per-connection failover cooldown and probe state scoped by profile.
- `LoadbalanceEvent`: Persistent record of failover, recovery, and health transitions.

## CONVENTIONS
- Use `identity.py` for anything related to auth subjects, credentials, or profile containers.
- Use `routing.py` for the structural graph of models, endpoints, api-family compatibility, and their connections.
- Use `observability.py` for telemetry, logs, settings, and persisted loadbalance current-state or event persistence.
- Keep business logic out of models; use properties for simple derivations like secret masking.
- Ensure all new models are re-exported in `models/models.py` to maintain the public ORM boundary.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Prefer the best current implementation shape over preserving the old one. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS
- Do not add an ORM class to a domain file and forget to expose it from `models.py`.
- Do not move password-reset, session, or passkey persistence into the routing or observability domains just because those flows touch routers or logs.
- Do not treat observability tables as optional sidecars. Request logs, audit logs, and load-balance state are part of the backend runtime contract.
