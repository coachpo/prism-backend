# BACKEND MODELS KNOWLEDGE BASE

## OVERVIEW
ORM models for Prism, split into identity, routing, and observability domains. All models inherit from a shared `Base` and use SQLAlchemy 2.0 style mapped columns.

## STRUCTURE
```
models/
├── domains/
│   ├── identity.py       # Profiles, providers, operator auth, refresh tokens, proxy keys, WebAuthn
│   ├── routing.py        # Model configs, endpoints, pricing templates, connections
│   └── observability.py  # Request logs, user settings, FX rates, blocklist rules, audit logs, loadbalance events
└── models.py             # Re-export boundary for all domain models
```

## DOMAINS

### IDENTITY
- `Profile`: Multi-profile scope container; owns `is_active` and `is_default` flags.
- `Provider`: Global seed rows for `openai`, `anthropic`, `gemini`.
- `AppAuthSettings`: Singleton operator auth state, username/email, and token version.
- `RefreshToken`: Session persistence with rotation and revocation.
- `ProxyApiKey`: Runtime data-plane credentials with prefix/hash storage.
- `WebAuthnChallenge` & `WebAuthnCredential`: Passkey lifecycle and device metadata.

### ROUTING
- `ModelConfig`: Per-profile model settings, failover cooldowns, and redirect targets.
- `Endpoint`: Encrypted upstream base URLs and API keys.
- `PricingTemplate`: Costing rules for input, output, reasoning, and cache tokens.
- `Connection`: Linkage between model configs, endpoints, and pricing; owns health status.

### OBSERVABILITY
- `RequestLog`: High-volume telemetry for all proxy traffic; owns billable and priced flags.
- `UserSetting`: Per-profile currency and timezone preferences.
- `EndpointFxRateSetting`: Custom FX rates for specific model/endpoint pairs.
- `HeaderBlocklistRule`: System and profile-scoped rules for stripping sensitive headers.
- `AuditLog`: Detailed request/response capture for audited providers.
- `LoadbalanceEvent`: Persistent record of failover, recovery, and health transitions.

## CONVENTIONS
- Use `identity.py` for anything related to auth subjects, credentials, or profile containers.
- Use `routing.py` for the structural graph of models, endpoints, and their connections.
- Use `observability.py` for telemetry, logs, settings, and runtime event persistence.
- Keep business logic out of models; use properties for simple derivations like secret masking.
- Ensure all new models are re-exported in `models/models.py` to maintain the public ORM boundary.
