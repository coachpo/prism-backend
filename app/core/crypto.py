import base64
import hashlib
import hmac
import secrets
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

_ENCRYPTED_PREFIX: Final[str] = "enc:"
_BUNDLE_CIPHER: Final[str] = "fernet-v1"
_password_hasher = PasswordHasher()


def _build_fernet_from_key(raw_key: str) -> Fernet:
    raw_key_bytes = raw_key.encode("utf-8")
    derived_key = hashlib.sha256(raw_key_bytes).digest()
    return Fernet(base64.urlsafe_b64encode(derived_key))


def _build_fernet() -> Fernet:
    raw_key = get_settings().secret_encryption_key
    return _build_fernet_from_key(raw_key)


def _get_bundle_encryption_key() -> str:
    settings = get_settings()
    return settings.config_bundle_encryption_key or settings.secret_encryption_key


def _build_bundle_fernet() -> Fernet:
    return _build_fernet_from_key(_get_bundle_encryption_key())


def get_bundle_secret_cipher() -> str:
    return _BUNDLE_CIPHER


def get_bundle_secret_key_id() -> str:
    raw_key = _get_bundle_encryption_key().encode("utf-8")
    derived_key = hashlib.sha256(raw_key).digest()
    return f"sha256:{derived_key.hex()}"


def encrypt_secret(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    if normalized.startswith(_ENCRYPTED_PREFIX):
        return normalized
    token = _build_fernet().encrypt(normalized.encode("utf-8")).decode("utf-8")
    return f"{_ENCRYPTED_PREFIX}{token}"


def decrypt_secret(value: object | None) -> str:
    if value is None or not isinstance(value, str):
        return ""
    normalized = value.strip()
    if not normalized:
        return ""
    if not normalized.startswith(_ENCRYPTED_PREFIX):
        return normalized
    encrypted = normalized[len(_ENCRYPTED_PREFIX) :]
    try:
        return _build_fernet().decrypt(encrypted.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Stored secret could not be decrypted") from exc


def encrypt_bundle_secret(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    if normalized.startswith(_ENCRYPTED_PREFIX):
        return normalized
    token = _build_bundle_fernet().encrypt(normalized.encode("utf-8")).decode("utf-8")
    return f"{_ENCRYPTED_PREFIX}{token}"


def decrypt_bundle_secret(value: object | None) -> str:
    if value is None or not isinstance(value, str):
        return ""
    normalized = value.strip()
    if not normalized:
        return ""
    if not normalized.startswith(_ENCRYPTED_PREFIX):
        return normalized
    encrypted = normalized[len(_ENCRYPTED_PREFIX) :]
    try:
        return _build_bundle_fernet().decrypt(encrypted.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Bundle secret could not be decrypted") from exc


def mask_secret(value: str | None) -> str | None:
    decrypted = decrypt_secret(value)
    if not decrypted:
        return None
    if len(decrypted) <= 8:
        return "*" * len(decrypted)
    return f"{'*' * 8}{decrypted[-4:]}"


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def hash_opaque_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_opaque_token(value: str, expected_hash: str) -> bool:
    actual_hash = hash_opaque_token(value)
    return hmac.compare_digest(actual_hash, expected_hash)


def generate_otp_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"
