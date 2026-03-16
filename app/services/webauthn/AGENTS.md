# WEBAUTHN SERVICE KNOWLEDGE BASE

## OVERVIEW
Implementation of WebAuthn/FIDO2 passkey lifecycle, split into registration, authentication, and credential management. It uses `PyWebAuthn` for core validation and handles challenge persistence and device metadata.

## STRUCTURE
```
webauthn/
├── registration.py    # Passkey creation and verification
├── authentication.py  # Passkey login and assertion verification
├── credentials.py     # Credential CRUD and device metadata
└── common.py          # Shared helpers for challenge handling and encoding
```

## WORKFLOWS

### REGISTRATION
- Generate options via `registration.py`.
- Persist challenge in `webauthn_challenges`.
- Verify response and store `WebAuthnCredential` with public key and AAGUID.

### AUTHENTICATION
- Generate assertion options via `authentication.py`.
- Verify signature against stored public key.
- Update `sign_count`, `last_used_at`, and `last_used_ip`.

### MANAGEMENT
- List registered credentials for the operator.
- Revoke/delete credentials.

## CONVENTIONS
- Keep challenge persistence in `common.py` or the shared `webauthn_challenges` table.
- Use `registration.py` for the initial binding of a passkey to the operator account.
- Use `authentication.py` for subsequent logins.
- Ensure `sign_count` is always validated to prevent cloned-authenticator attacks.
- Re-export public methods through `app/services/webauthn_service.py`.
