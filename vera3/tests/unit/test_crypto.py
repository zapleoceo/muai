"""Тесты encryption токенов."""
from __future__ import annotations

import pytest

from vera_shared.tokens.crypto import ENCRYPTED_PREFIX, decrypt, encrypt, is_encrypted


SECRET = "test-secret-key-for-encryption-32bytes-long-enough"


def test_encrypt_returns_versioned_format():
    encrypted = encrypt("my-api-key-12345", secret=SECRET)
    assert encrypted.startswith(ENCRYPTED_PREFIX)


def test_decrypt_returns_original():
    plain = "csk-abcdef123456"
    encrypted = encrypt(plain, secret=SECRET)
    assert decrypt(encrypted, secret=SECRET) == plain


def test_decrypt_legacy_plaintext_passes_through():
    # Legacy токены из миграции — без префикса. Возвращаются как есть.
    plain_legacy = "sk-old-format-no-prefix"
    assert decrypt(plain_legacy, secret=SECRET) == plain_legacy


def test_is_encrypted():
    assert is_encrypted("enc1:abc123") is True
    assert is_encrypted("sk-plain") is False
    assert is_encrypted("") is False


def test_decrypt_with_wrong_secret_raises():
    encrypted = encrypt("secret-data", secret=SECRET)
    with pytest.raises(Exception):
        decrypt(encrypted, secret="completely-different-secret")


def test_encrypt_empty_secret_raises():
    with pytest.raises(ValueError):
        encrypt("data", secret="")


def test_encrypt_two_invocations_produce_different_output():
    # Fernet добавляет IV → одинаковый input даёт разный output
    a = encrypt("same-input", secret=SECRET)
    b = encrypt("same-input", secret=SECRET)
    assert a != b
    # Но оба расшифровываются корректно
    assert decrypt(a, secret=SECRET) == decrypt(b, secret=SECRET) == "same-input"


def test_decrypt_modified_payload_fails():
    encrypted = encrypt("secret", secret=SECRET)
    # Изменяем один байт
    bad = encrypted[:-5] + "XXXXX"
    with pytest.raises(Exception):
        decrypt(bad, secret=SECRET)
