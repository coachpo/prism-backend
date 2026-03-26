# BACKEND SETTINGS DOMAINS KNOWLEDGE BASE

## OVERVIEW
`settings_domains/` is the split settings-management package behind `../settings.py`. It owns global auth settings, profile-scoped costing and timezone settings, email-verification flows, proxy API key management, and helper access to per-profile user-settings rows.

## STRUCTURE
```
settings_domains/
├── auth_settings_route_handlers.py      # Global auth-settings read/update routes
├── costing_route_handlers.py            # Profile-scoped costing and timezone routes
├── email_verification_route_handlers.py # Verification request and confirm routes
├── proxy_key_route_handlers.py          # Proxy API key list/create/rotate/delete routes
└── helpers.py                           # User-settings bootstrap and auth-subject extraction helpers
```

## WHERE TO LOOK

- Global auth-settings responses: `auth_settings_route_handlers.py`
- Costing and timezone responses: `costing_route_handlers.py`
- Email-verification request/confirm flows: `email_verification_route_handlers.py`
- Proxy API key CRUD and rotation routes: `proxy_key_route_handlers.py`
- Shared per-profile user-settings bootstrap and request auth-subject extraction: `helpers.py`

## CONVENTIONS

- Keep `settings.py` as a composition router that mounts the package routers.
- Keep global auth settings separate from profile-scoped costing and timezone state.
- Reuse `get_or_create_user_settings()` instead of duplicating profile-settings bootstrap in handlers.
- Keep proxy API key management in this package rather than scattering it across auth routes.

## ANTI-PATTERNS

- Do not blur global auth settings with selected-profile settings behavior.
- Do not re-implement request auth-subject extraction outside `helpers.py`.
- Do not move costing, timezone, or proxy-key route logic back into `settings.py`.
