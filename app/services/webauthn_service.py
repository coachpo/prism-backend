"""WebAuthn service for Passkey authentication.

This module provides core WebAuthn functionality for Passkey registration and authentication.
Uses py_webauthn library for WebAuthn protocol implementation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    CredentialDeviceType,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.core.config import get_settings
from app.core.time import utc_now
from app.models.domains.identity import WebAuthnCredential


# Challenge storage (in-memory for now, should use Redis in production)
_challenge_store: dict[str, tuple[bytes, datetime]] = {}
_AUTHENTICATION_CHALLENGE_KEY = "authentication"


def _get_rp_id() -> str:
    """Get Relying Party ID from settings."""
    settings = get_settings()
    return settings.webauthn_rp_id


def _get_rp_name() -> str:
    """Get Relying Party name from settings."""
    settings = get_settings()
    return settings.webauthn_rp_name


def _get_origin() -> str:
    """Get expected origin for WebAuthn operations."""
    settings = get_settings()
    return settings.webauthn_origin


def _store_challenge(user_id: str, challenge: bytes) -> None:
    """Store challenge temporarily (2 minutes TTL)."""
    expires_at = utc_now() + timedelta(minutes=2)
    _challenge_store[user_id] = (challenge, expires_at)


def _get_challenge(user_id: str) -> bytes | None:
    """Retrieve and validate stored challenge."""
    if user_id not in _challenge_store:
        return None

    challenge, expires_at = _challenge_store[user_id]
    if utc_now() > expires_at:
        del _challenge_store[user_id]
        return None

    return challenge


def _clear_challenge(user_id: str) -> None:
    """Clear stored challenge after use."""
    _challenge_store.pop(user_id, None)


def _serialize_aaguid(aaguid: str | None) -> bytes | None:
    if not aaguid:
        return None
    try:
        return UUID(aaguid).bytes
    except ValueError:
        return None


async def generate_registration_options_for_user(
    db: AsyncSession,
    auth_subject_id: int,
    username: str,
) -> dict[str, Any]:
    """Generate WebAuthn registration options for a user.

    Args:
        db: Database session
        auth_subject_id: User's auth subject ID
        username: Username for display

    Returns:
        Registration options dict compatible with @simplewebauthn/browser
    """
    # Get existing credentials to exclude
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

    # Generate registration options
    options = generate_registration_options(
        rp_id=_get_rp_id(),
        rp_name=_get_rp_name(),
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

    # Store challenge
    _store_challenge(str(auth_subject_id), options.challenge)

    # Convert to dict for JSON serialization
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
) -> WebAuthnCredential:
    """Verify registration response and save credential.

    Args:
        db: Database session
        auth_subject_id: User's auth subject ID
        credential: Registration credential from client
        device_name: Optional device name for management

    Returns:
        Created WebAuthnCredential

    Raises:
        ValueError: If verification fails
    """
    # Retrieve stored challenge
    expected_challenge = _get_challenge(str(auth_subject_id))
    if not expected_challenge:
        raise ValueError("Challenge not found or expired")

    try:
        # Verify registration
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=expected_challenge,
            expected_origin=_get_origin(),
            expected_rp_id=_get_rp_id(),
            require_user_verification=True,
        )

        # Clear challenge after successful verification
        _clear_challenge(str(auth_subject_id))

        # Save credential to database
        new_credential = WebAuthnCredential(
            auth_subject_id=auth_subject_id,
            credential_id=verification.credential_id,
            public_key=verification.credential_public_key,
            sign_count=verification.sign_count,
            device_name=device_name or "Unnamed Device",
            aaguid=_serialize_aaguid(verification.aaguid),
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
        _clear_challenge(str(auth_subject_id))
        raise ValueError(f"Registration verification failed: {str(e)}") from e


async def generate_authentication_options_for_user(
    db: AsyncSession,
    auth_subject_id: int | None = None,
) -> dict[str, Any]:
    """Generate WebAuthn authentication options.

    Args:
        db: Database session
        auth_subject_id: Optional user ID for user-specific auth

    Returns:
        Authentication options dict compatible with @simplewebauthn/browser
    """
    allow_credentials = []
    user_verification_requirement = UserVerificationRequirement.REQUIRED

    if auth_subject_id:
        # User-specific authentication
        stmt = select(WebAuthnCredential).where(
            WebAuthnCredential.auth_subject_id == auth_subject_id
        )
        result = await db.execute(stmt)
        credentials = result.scalars().all()

        allow_credentials = [
            PublicKeyCredentialDescriptor(id=cred.credential_id) for cred in credentials
        ]

    # Generate authentication options
    options = generate_authentication_options(
        rp_id=_get_rp_id(),
        allow_credentials=allow_credentials if allow_credentials else None,
        user_verification=user_verification_requirement,
    )

    # Store challenge
    _store_challenge(_AUTHENTICATION_CHALLENGE_KEY, options.challenge)

    # Convert to dict for JSON serialization
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
) -> tuple[WebAuthnCredential, int]:
    """Verify authentication response.

    Args:
        db: Database session
        credential: Authentication credential from client
        auth_subject_id: Optional user ID for verification

    Returns:
        Tuple of (WebAuthnCredential, auth_subject_id)

    Raises:
        ValueError: If verification fails
    """
    # Retrieve stored challenge
    expected_challenge = _get_challenge(_AUTHENTICATION_CHALLENGE_KEY)
    if not expected_challenge:
        raise ValueError("Challenge not found or expired")

    try:
        # Find credential in database
        credential_id = base64url_to_bytes(credential["rawId"])
        stmt = select(WebAuthnCredential).where(
            WebAuthnCredential.credential_id == credential_id
        )
        result = await db.execute(stmt)
        db_credential = result.scalar_one_or_none()

        if not db_credential:
            raise ValueError("Credential not found")

        # Verify authentication
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=expected_challenge,
            expected_origin=_get_origin(),
            expected_rp_id=_get_rp_id(),
            credential_public_key=db_credential.public_key,
            credential_current_sign_count=db_credential.sign_count,
            require_user_verification=True,
        )

        # Clear challenge after successful verification
        _clear_challenge(_AUTHENTICATION_CHALLENGE_KEY)

        # Check for sign count anomaly (potential cloned credential)
        if verification.new_sign_count <= db_credential.sign_count:
            # Log anomaly but don't block (some authenticators don't increment)
            # In production, this should trigger an audit log event
            pass

        # Update credential
        db_credential.sign_count = verification.new_sign_count
        db_credential.last_used_at = utc_now()
        db_credential.last_used_ip = client_ip
        db_credential.backup_state = verification.credential_backed_up
        await db.flush()

        return db_credential, db_credential.auth_subject_id

    except Exception as e:
        _clear_challenge(_AUTHENTICATION_CHALLENGE_KEY)
        raise ValueError(f"Authentication verification failed: {str(e)}") from e


async def list_credentials_for_user(
    db: AsyncSession,
    auth_subject_id: int,
) -> list[WebAuthnCredential]:
    """List all credentials for a user.

    Args:
        db: Database session
        auth_subject_id: User's auth subject ID

    Returns:
        List of WebAuthnCredential
    """
    stmt = (
        select(WebAuthnCredential)
        .where(WebAuthnCredential.auth_subject_id == auth_subject_id)
        .order_by(WebAuthnCredential.created_at.desc())
    )

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def list_credentials(
    db: AsyncSession,
    auth_subject_id: int,
) -> list[WebAuthnCredential]:
    """Backward-compatible alias for list_credentials_for_user."""
    return await list_credentials_for_user(db, auth_subject_id=auth_subject_id)


async def revoke_credential(
    db: AsyncSession,
    credential_id: int,
    auth_subject_id: int,
) -> bool:
    """Revoke (delete) a credential.

    Args:
        db: Database session
        credential_id: Credential ID to revoke
        auth_subject_id: User's auth subject ID (for authorization)

    Returns:
        True if revoked, False if not found
    """
    stmt = select(WebAuthnCredential).where(
        WebAuthnCredential.id == credential_id,
        WebAuthnCredential.auth_subject_id == auth_subject_id,
    )
    result = await db.execute(stmt)
    credential = result.scalar_one_or_none()

    if not credential:
        return False

    await db.delete(credential)
    await db.flush()
    return True
