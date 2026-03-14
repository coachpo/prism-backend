# BACKEND AUTH SERVICES KNOWLEDGE BASE

## OVERVIEW
`services/auth/` is the split auth package behind `../auth_service.py`: singleton auth settings, session lifecycle, password-reset OTPs, email delivery, proxy API keys, and verified-email gating.

## STRUCTURE
```
auth/
├── app_settings.py    # Singleton auth-settings row lookup and password requirements
├── email_delivery.py  # SMTP-backed email verification and password-reset sends
├── password_reset.py  # OTP challenge creation and consumption
├── proxy_keys.py      # Proxy key limit, create, rotate, delete, verify, serialize
├── sessions.py        # Login, session creation, refresh rotation, token-family revocation
└── settings.py        # Auth enablement, email verification state, response shaping
```

## WHERE TO LOOK

- Public re-export boundary: `../auth_service.py`, `__init__.py`
- Singleton auth settings and password gating: `app_settings.py`
- Auth enablement, verified-email flow, response shaping: `settings.py`
- Login/session creation and refresh-token family operations: `sessions.py`
- Password-reset challenge lifecycle: `password_reset.py`
- Proxy API key CRUD and verification: `proxy_keys.py`
- SMTP send paths: `email_delivery.py`

## CONVENTIONS

- Treat `../auth_service.py` as the public import surface; new callers should not reach into leaf modules without a good reason.
- Keep auth-setting mutations and verified-email requirements in `settings.py` and `app_settings.py` rather than scattering them across routers.
- Keep proxy-key parsing, verification, limits, and serialization in `proxy_keys.py`.
- Keep refresh-token rotation and family revocation in `sessions.py` so router handlers stay thin.
- Keep email side effects in `email_delivery.py`; password-reset and email-verification flows should call into it instead of opening SMTP clients elsewhere.

## ANTI-PATTERNS

- Do not duplicate proxy-key verification or preview formatting outside `proxy_keys.py`.
- Do not bypass `get_or_create_app_auth_settings()` when auth-setting state is required.
- Do not re-implement password-reset or email-verification challenge logic in routers.
- Do not spread refresh-token family mutation across unrelated modules; session lifecycle belongs in `sessions.py`.
