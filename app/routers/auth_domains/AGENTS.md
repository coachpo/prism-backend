# BACKEND AUTH DOMAINS KNOWLEDGE BASE

## OVERVIEW
`auth_domains/` is the route-helper package behind `../auth.py`. It owns cookie helpers, auth status and session bootstrap flows, password-reset response builders, and WebAuthn registration, authentication, and credential handlers.

## STRUCTURE
```
auth_domains/
├── session_route_handlers.py          # Auth status, public bootstrap, login, logout, refresh, current session
├── password_reset_route_handlers.py   # Password-reset request and confirm responses
├── webauthn_route_handlers.py         # Registration/authentication options and verify flows, credential list/revoke
└── cookie_helpers.py                  # Set and clear auth cookies for the shell router
```

## WHERE TO LOOK

- Session bootstrap, login/logout, refresh, and current-session responses: `session_route_handlers.py`
- Password-reset request and confirm flows: `password_reset_route_handlers.py`
- WebAuthn registration, authentication, and credential-management responses: `webauthn_route_handlers.py`
- Cookie set/clear helpers passed through `auth.py`: `cookie_helpers.py`

## CONVENTIONS

- Keep `auth.py` thin. Route methods should hand work to this package instead of rebuilding auth flows inline.
- Keep cookie mutation centralized in `cookie_helpers.py` so session and password-reset flows reuse one contract.
- Keep password-reset response shaping separate from session and WebAuthn handlers.
- Keep WebAuthn request handling here and leave deeper passkey persistence or verification to `../../services/webauthn/`.

## ANTI-PATTERNS

- Do not set or clear auth cookies ad hoc in `auth.py`.
- Do not re-implement password-reset orchestration in the shell router.
- Do not mix passkey and session route logic into one handler file when the package already splits them.
