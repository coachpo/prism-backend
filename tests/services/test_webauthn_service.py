"""Unit tests for WebAuthn service."""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import CredentialDeviceType

from app.services import webauthn_service
from app.models.domains.identity import WebAuthnCredential


@pytest.fixture(autouse=True)
def clear_challenge_store():
    webauthn_service._challenge_store.clear()
    yield
    webauthn_service._challenge_store.clear()


@pytest.mark.asyncio
async def test_generate_registration_options():
    """Test generating registration options."""
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        )
    )

    options = await webauthn_service.generate_registration_options_for_user(
        db, auth_subject_id=1, username="testuser"
    )

    assert "challenge" in options
    assert "rp" in options
    assert "user" in options
    assert options["rp"]["id"] == "localhost"
    assert options["user"]["name"] == "testuser"


@pytest.mark.asyncio
async def test_verify_registration_missing_challenge():
    """Test registration verification fails without challenge."""
    db = AsyncMock()

    with pytest.raises(ValueError, match="Challenge not found or expired"):
        await webauthn_service.verify_and_save_registration(
            db,
            auth_subject_id=1,
            credential={
                "id": "test",
                "rawId": "test",
                "response": {},
                "type": "public-key",
            },
            device_name="Test Device",
        )


@pytest.mark.asyncio
async def test_verify_registration_passes_raw_credential_dict_to_webauthn():
    db = MagicMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    credential = {
        "id": "credential-id",
        "rawId": "credential-id",
        "response": {
            "clientDataJSON": "client-data",
            "attestationObject": "attestation-object",
        },
        "type": "public-key",
    }
    verification = SimpleNamespace(
        credential_id=b"credential-id",
        credential_public_key=b"public-key",
        sign_count=1,
        aaguid="00000000-0000-0000-0000-000000000000",
        credential_device_type=CredentialDeviceType.MULTI_DEVICE,
        credential_backed_up=False,
    )
    webauthn_service._store_challenge("1", b"registration-challenge")

    with patch.object(
        webauthn_service,
        "verify_registration_response",
        return_value=verification,
    ) as verify_mock:
        result = await webauthn_service.verify_and_save_registration(
            db,
            auth_subject_id=1,
            credential=credential,
            device_name="Laptop",
        )

    assert verify_mock.call_args.kwargs["credential"] is credential
    assert (
        verify_mock.call_args.kwargs["expected_challenge"] == b"registration-challenge"
    )
    assert result.auth_subject_id == 1
    assert result.device_name == "Laptop"
    assert result.aaguid == UUID("00000000-0000-0000-0000-000000000000").bytes
    assert result.backup_eligible is True
    assert webauthn_service._get_challenge("1") is None


@pytest.mark.asyncio
async def test_verify_authentication_uses_shared_challenge_key():
    options_db = AsyncMock()
    options_db.execute = AsyncMock(
        return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        )
    )
    verify_db = AsyncMock()
    db_credential = SimpleNamespace(
        credential_id=b"credential-id",
        public_key=b"public-key",
        sign_count=7,
        auth_subject_id=1,
        last_used_at=None,
        last_used_ip=None,
        backup_state=False,
    )
    verify_db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=db_credential))
    )
    verification = SimpleNamespace(new_sign_count=8, credential_backed_up=True)
    raw_id = bytes_to_base64url(b"credential-id")
    credential = {
        "id": raw_id,
        "rawId": raw_id,
        "response": {
            "clientDataJSON": "client-data",
            "authenticatorData": "authenticator-data",
            "signature": "signature",
            "userHandle": "",
        },
        "type": "public-key",
    }

    options = await webauthn_service.generate_authentication_options_for_user(
        options_db,
        auth_subject_id=1,
    )

    assert webauthn_service._get_challenge(
        webauthn_service._AUTHENTICATION_CHALLENGE_KEY
    ) == webauthn_service.base64url_to_bytes(options["challenge"])
    assert webauthn_service._get_challenge("1") is None

    with patch.object(
        webauthn_service,
        "verify_authentication_response",
        return_value=verification,
    ) as verify_mock:
        (
            result_credential,
            auth_subject_id,
        ) = await webauthn_service.verify_authentication(
            verify_db,
            credential=credential,
            auth_subject_id=None,
            client_ip="127.0.0.1",
        )

    assert verify_mock.call_args.kwargs["credential"] is credential
    assert verify_mock.call_args.kwargs[
        "expected_challenge"
    ] == webauthn_service.base64url_to_bytes(options["challenge"])
    assert auth_subject_id == 1
    assert result_credential.sign_count == 8
    assert result_credential.last_used_ip == "127.0.0.1"
    assert result_credential.backup_state is True
    assert (
        webauthn_service._get_challenge(webauthn_service._AUTHENTICATION_CHALLENGE_KEY)
        is None
    )


@pytest.mark.asyncio
async def test_list_credentials():
    """Test listing user credentials."""
    db = AsyncMock()
    mock_creds = [
        MagicMock(
            id=1,
            device_name="Device 1",
            backup_eligible=True,
            backup_state=False,
            last_used_at=None,
            created_at="2026-03-12T00:00:00Z",
        )
    ]
    db.execute = AsyncMock(
        return_value=MagicMock(
            scalars=MagicMock(
                return_value=MagicMock(all=MagicMock(return_value=mock_creds))
            )
        )
    )

    credentials = await webauthn_service.list_credentials_for_user(
        db, auth_subject_id=1
    )

    assert len(credentials) == 1
    assert credentials[0].device_name == "Device 1"


@pytest.mark.asyncio
async def test_revoke_credential_success():
    """Test revoking a credential."""
    db = AsyncMock()
    mock_cred = MagicMock(spec=WebAuthnCredential)
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=mock_cred))
    )

    success = await webauthn_service.revoke_credential(
        db, credential_id=1, auth_subject_id=1
    )

    assert success is True
    db.delete.assert_called_once_with(mock_cred)


@pytest.mark.asyncio
async def test_revoke_credential_not_found():
    """Test revoking non-existent credential."""
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    )

    success = await webauthn_service.revoke_credential(
        db, credential_id=999, auth_subject_id=1
    )

    assert success is False


@pytest.mark.asyncio
async def test_challenge_expiration():
    """Test challenge expiration logic."""
    # Store a challenge
    webauthn_service._store_challenge("user1", b"test_challenge")

    # Retrieve immediately - should work
    challenge = webauthn_service._get_challenge("user1")
    assert challenge == b"test_challenge"

    # Clear challenge
    webauthn_service._clear_challenge("user1")

    # Should be gone
    challenge = webauthn_service._get_challenge("user1")
    assert challenge is None
