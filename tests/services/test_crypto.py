from app.core.crypto import encrypt_secret, mask_secret


def test_mask_secret_reveals_only_the_last_four_characters_for_long_values():
    secret = "sk-1234567890abcdefghijklmnop"

    assert mask_secret(encrypt_secret(secret)) == "********mnop"


def test_mask_secret_reveals_only_the_last_four_characters_for_mid_length_values():
    secret = "123456789012"

    assert mask_secret(encrypt_secret(secret)) == "********9012"


def test_mask_secret_fully_redacts_values_of_four_characters_or_less():
    secret = "1234"

    assert mask_secret(encrypt_secret(secret)) == "****"


def test_mask_secret_fully_redacts_values_of_eight_characters_or_less():
    secret = "12345678"

    assert mask_secret(encrypt_secret(secret)) == "********"
