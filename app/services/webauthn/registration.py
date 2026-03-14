from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_registration_options,
    verify_registration_response as _verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    CredentialDeviceType,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.models.domains.identity import WebAuthnCredential

from .common import (
    clear_challenge,
    get_challenge,
    get_origin,
    get_rp_id,
    get_rp_name,
    serialize_aaguid,
    store_challenge,
)


async def generate_registration_options_for_user(
    db: AsyncSession,
    auth_subject_id: int,
    username: str,
    *,
    get_rp_id_fn=get_rp_id,
    get_rp_name_fn=get_rp_name,
    store_challenge_fn=store_challenge,
) -> dict[str, Any]:
    stmt = select(WebAuthnCredential).where(
        WebAuthnCredential.auth_subject_id == auth_subject_id
    )
    result = await db.execute(stmt)
    existing_credentials = result.scalars().all()

    exclude_credentials = [
        PublicKeyCredentialDescriptor(id=cred.credential_id)
        for cred in existing_credentials
    ]
    resident_key_requirement = ResidentKeyRequirement.REQUIRED
    user_verification_requirement = UserVerificationRequirement.REQUIRED
    attestation_preference = AttestationConveyancePreference.NONE

    options = generate_registration_options(
        rp_id=get_rp_id_fn(),
        rp_name=get_rp_name_fn(),
        user_id=str(auth_subject_id).encode(),
        user_name=username,
        user_display_name=username,
        exclude_credentials=exclude_credentials,
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=user_verification_requirement,
            resident_key=resident_key_requirement,
        ),
        attestation=attestation_preference,
    )

    await store_challenge_fn(db, str(auth_subject_id), options.challenge)

    return {
        "challenge": bytes_to_base64url(options.challenge),
        "rp": {"id": options.rp.id, "name": options.rp.name},
        "user": {
            "id": bytes_to_base64url(options.user.id),
            "name": options.user.name,
            "displayName": options.user.display_name,
        },
        "pubKeyCredParams": [
            {"type": param.type, "alg": param.alg}
            for param in options.pub_key_cred_params
        ],
        "timeout": options.timeout,
        "excludeCredentials": [
            {
                "id": bytes_to_base64url(cred.id),
                "type": cred.type,
                "transports": cred.transports or [],
            }
            for cred in (options.exclude_credentials or [])
        ],
        "authenticatorSelection": {
            "userVerification": user_verification_requirement.value,
            "residentKey": resident_key_requirement.value,
        },
        "attestation": attestation_preference.value,
    }


async def verify_and_save_registration(
    db: AsyncSession,
    auth_subject_id: int,
    credential: dict[str, Any],
    device_name: str | None = None,
    *,
    clear_challenge_fn=clear_challenge,
    get_challenge_fn=get_challenge,
    get_origin_fn=get_origin,
    get_rp_id_fn=get_rp_id,
    serialize_aaguid_fn=serialize_aaguid,
    verify_registration_response_fn=_verify_registration_response,
) -> WebAuthnCredential:
    expected_challenge = await get_challenge_fn(db, str(auth_subject_id))
    if not expected_challenge:
        raise ValueError("Challenge not found or expired")

    try:
        verification = verify_registration_response_fn(
            credential=credential,
            expected_challenge=expected_challenge,
            expected_origin=get_origin_fn(),
            expected_rp_id=get_rp_id_fn(),
            require_user_verification=True,
        )

        await clear_challenge_fn(db, str(auth_subject_id))

        new_credential = WebAuthnCredential(
            auth_subject_id=auth_subject_id,
            credential_id=verification.credential_id,
            public_key=verification.credential_public_key,
            sign_count=verification.sign_count,
            device_name=device_name or "Unnamed Device",
            aaguid=serialize_aaguid_fn(verification.aaguid),
            backup_eligible=(
                verification.credential_device_type == CredentialDeviceType.MULTI_DEVICE
            ),
            backup_state=verification.credential_backed_up,
        )

        db.add(new_credential)
        await db.flush()
        await db.refresh(new_credential)

        return new_credential

    except Exception as e:
        await clear_challenge_fn(db, str(auth_subject_id))
        raise ValueError(f"Registration verification failed: {str(e)}") from e


__all__ = [
    "generate_registration_options_for_user",
    "verify_and_save_registration",
]
