from webauthn import (
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes

from app.services.webauthn import (
    list_credentials_for_user,
    revoke_credential,
)
from app.services.webauthn.authentication import (
    generate_authentication_options_for_user as _generate_authentication_options_for_user,
)
from app.services.webauthn.authentication import (
    verify_authentication as _verify_authentication,
)
from app.services.webauthn.common import (
    AUTHENTICATION_CHALLENGE_KEY as _AUTHENTICATION_CHALLENGE_KEY,
    clear_challenge as _clear_challenge,
    get_challenge as _get_challenge,
    get_origin as _get_origin,
    get_rp_id as _get_rp_id,
    get_rp_name as _get_rp_name,
    serialize_aaguid as _serialize_aaguid,
    store_challenge as _store_challenge,
)
from app.services.webauthn.registration import (
    generate_registration_options_for_user as _generate_registration_options_for_user,
)
from app.services.webauthn.registration import (
    verify_and_save_registration as _verify_and_save_registration,
)


async def generate_registration_options_for_user(db, auth_subject_id, username):
    return await _generate_registration_options_for_user(
        db,
        auth_subject_id,
        username,
        get_rp_id_fn=_get_rp_id,
        get_rp_name_fn=_get_rp_name,
        store_challenge_fn=_store_challenge,
    )


async def verify_and_save_registration(
    db, auth_subject_id, credential, device_name=None
):
    return await _verify_and_save_registration(
        db,
        auth_subject_id,
        credential,
        device_name,
        clear_challenge_fn=_clear_challenge,
        get_challenge_fn=_get_challenge,
        get_origin_fn=_get_origin,
        get_rp_id_fn=_get_rp_id,
        serialize_aaguid_fn=_serialize_aaguid,
        verify_registration_response_fn=verify_registration_response,
    )


async def generate_authentication_options_for_user(db, auth_subject_id=None):
    return await _generate_authentication_options_for_user(
        db,
        auth_subject_id,
        get_rp_id_fn=_get_rp_id,
        store_challenge_fn=_store_challenge,
    )


async def verify_authentication(db, credential, auth_subject_id=None, client_ip=None):
    return await _verify_authentication(
        db,
        credential,
        auth_subject_id,
        client_ip,
        authentication_challenge_key=_AUTHENTICATION_CHALLENGE_KEY,
        base64url_to_bytes_fn=base64url_to_bytes,
        clear_challenge_fn=_clear_challenge,
        get_challenge_fn=_get_challenge,
        get_origin_fn=_get_origin,
        get_rp_id_fn=_get_rp_id,
        verify_authentication_response_fn=verify_authentication_response,
    )


__all__ = [
    "_AUTHENTICATION_CHALLENGE_KEY",
    "_clear_challenge",
    "_get_challenge",
    "_store_challenge",
    "base64url_to_bytes",
    "generate_authentication_options_for_user",
    "generate_registration_options_for_user",
    "list_credentials_for_user",
    "revoke_credential",
    "verify_authentication_response",
    "verify_registration_response",
    "verify_and_save_registration",
    "verify_authentication",
]
