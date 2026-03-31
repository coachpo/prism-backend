# BACKEND WEBAUTHN SERVICE KNOWLEDGE BASE

## OVERVIEW
`services/webauthn/` is the split passkey package behind `../webauthn_service.py`. It owns registration, authentication, credential management, and the shared challenge helpers used by those flows.

## STRUCTURE
```
webauthn/
├── registration.py    # Passkey creation and verification
├── authentication.py  # Passkey login and assertion verification
├── credentials.py     # Credential CRUD and device metadata
├── common.py          # Shared helpers for challenge handling and encoding
└── __init__.py
```

## WHERE TO LOOK

- Public re-export boundary: `../webauthn_service.py`, `__init__.py`
- Passkey registration and response verification: `registration.py`
- Passkey login and assertion verification: `authentication.py`
- Credential CRUD and device metadata: `credentials.py`
- Challenge persistence and shared encoding helpers: `common.py`

## CONVENTIONS

- Keep challenge persistence in `common.py` or the shared `webauthn_challenges` table.
- Use `registration.py` for the initial binding of a passkey to the operator account.
- Use `authentication.py` for subsequent logins.
- Ensure `sign_count` is always validated to prevent cloned-authenticator attacks.
- Re-export public methods through `../webauthn_service.py`.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS

- Do not duplicate challenge storage logic outside `common.py`.
- Do not mix registration and authentication flows in a single helper when the package already splits them.
- Do not bypass the public `webauthn_service.py` boundary from routers.
