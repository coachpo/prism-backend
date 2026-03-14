from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options,
    verify_authentication_response as _verify_authentication_response,
)
from webauthn.helpers import (
    base64url_to_bytes as _base64url_to_bytes,
    bytes_to_base64url,
)
from webauthn.helpers.structs import (
    PublicKeyCredentialDescriptor,
    UserVerificationRequirement,
)

from app.core.time import utc_now
from app.models.domains.identity import WebAuthnCredential

from .common import (
    AUTHENTICATION_CHALLENGE_KEY,
    clear_challenge,
    get_challenge,
    get_origin,
    get_rp_id,
    store_challenge,
)


async def generate_authentication_options_for_user(
    db: AsyncSession,
    auth_subject_id: int | None = None,
    *,
    get_rp_id_fn=get_rp_id,
    store_challenge_fn=store_challenge,
) -> dict[str, Any]:
    allow_credentials = []
    user_verification_requirement = UserVerificationRequirement.REQUIRED

    if auth_subject_id:
        stmt = select(WebAuthnCredential).where(
            WebAuthnCredential.auth_subject_id == auth_subject_id
        )
        result = await db.execute(stmt)
        credentials = result.scalars().all()

        allow_credentials = [
            PublicKeyCredentialDescriptor(id=cred.credential_id) for cred in credentials
        ]

    options = generate_authentication_options(
        rp_id=get_rp_id_fn(),
        allow_credentials=allow_credentials if allow_credentials else None,
        user_verification=user_verification_requirement,
    )

    await store_challenge_fn(db, AUTHENTICATION_CHALLENGE_KEY, options.challenge)

    return {
        "challenge": bytes_to_base64url(options.challenge),
        "timeout": options.timeout,
        "rpId": options.rp_id,
        "allowCredentials": [
            {
                "id": bytes_to_base64url(cred.id),
                "type": cred.type,
                "transports": cred.transports or [],
            }
            for cred in (options.allow_credentials or [])
        ],
        "userVerification": user_verification_requirement.value,
    }


async def verify_authentication(
    db: AsyncSession,
    credential: dict[str, Any],
    auth_subject_id: int | None = None,
    client_ip: str | None = None,
    *,
    authentication_challenge_key: str = AUTHENTICATION_CHALLENGE_KEY,
    base64url_to_bytes_fn=_base64url_to_bytes,
    clear_challenge_fn=clear_challenge,
    get_challenge_fn=get_challenge,
    get_origin_fn=get_origin,
    get_rp_id_fn=get_rp_id,
    verify_authentication_response_fn=_verify_authentication_response,
) -> tuple[WebAuthnCredential, int]:
    expected_challenge = await get_challenge_fn(db, authentication_challenge_key)
    if not expected_challenge:
        raise ValueError("Challenge not found or expired")

    try:
        credential_id = base64url_to_bytes_fn(credential["rawId"])
        stmt = select(WebAuthnCredential).where(
            WebAuthnCredential.credential_id == credential_id
        )
        result = await db.execute(stmt)
        db_credential = result.scalar_one_or_none()

        if not db_credential:
            raise ValueError("Credential not found")

        verification = verify_authentication_response_fn(
            credential=credential,
            expected_challenge=expected_challenge,
            expected_origin=get_origin_fn(),
            expected_rp_id=get_rp_id_fn(),
            credential_public_key=db_credential.public_key,
            credential_current_sign_count=db_credential.sign_count,
            require_user_verification=True,
        )

        await clear_challenge_fn(db, authentication_challenge_key)

        if verification.new_sign_count <= db_credential.sign_count:
            pass

        db_credential.sign_count = verification.new_sign_count
        db_credential.last_used_at = utc_now()
        db_credential.last_used_ip = client_ip
        db_credential.backup_state = verification.credential_backed_up
        await db.flush()

        return db_credential, db_credential.auth_subject_id

    except Exception as e:
        await clear_challenge_fn(db, authentication_challenge_key)
        raise ValueError(f"Authentication verification failed: {str(e)}") from e


__all__ = [
    "generate_authentication_options_for_user",
    "verify_authentication",
]
