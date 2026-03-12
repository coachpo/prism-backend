"""Unit tests for WebAuthn service."""

import pytest
from unittest.mock import AsyncMock, MagicMock

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

    credentials = await webauthn_service.list_credentials_for_user(db, auth_subject_id=1)

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
