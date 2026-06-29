"""Тесты encryption токенов."""
from __future__ import annotations

import pytest
from cryptography.fernet import InvalidToken
from vera_shared.crypto import ENCRYPTED_PREFIX, decrypt, encrypt, is_encrypted

SECRET = "test-secret-key-for-encryption-32bytes-long-enough"


def test_encrypt_returns_versioned_format():
    encrypted = encrypt("my-api-key-12345", secret=SECRET)
    assert encrypted.startswith(ENCRYPTED_PREFIX)


def test_decrypt_returns_original():
    plain = "csk-abcdef123456"
    encrypted = encrypt(plain, secret=SECRET)
    assert decrypt(encrypted, secret=SECRET) == plain


def test_decrypt_plaintext_rejected_by_default(monkeypatch):
    # Security fix: plaintext без префикса теперь отклоняется по умолчанию —
    # закрывает вектор «атакующий с DB write подсовывает свой sk-...»
    monkeypatch.delenv("ALLOW_PLAINTEXT_TOKENS", raising=False)
    with pytest.raises(InvalidToken):
        decrypt("sk-old-format-no-prefix", secret=SECRET)


def test_decrypt_plaintext_allowed_with_migration_flag(monkeypatch):
    # Для одноразовой legacy-миграции — явный opt-in через env
    monkeypatch.setenv("ALLOW_PLAINTEXT_TOKENS", "1")
    plain_legacy = "sk-old-format-no-prefix"
    assert decrypt(plain_legacy, secret=SECRET) == plain_legacy


def test_is_encrypted():
    assert is_encrypted("enc1:abc123") is True
    assert is_encrypted("sk-plain") is False
    assert is_encrypted("") is False


def test_decrypt_with_wrong_secret_raises():
    encrypted = encrypt("secret-data", secret=SECRET)
    with pytest.raises(InvalidToken):
        decrypt(encrypted, secret="completely-different-secret")


def test_encrypt_empty_secret_raises(monkeypatch):
    # secret="" falls back на token_secret() из env — чистим env чтобы
    # проверить именно отказ при полном отсутствии секрета
    monkeypatch.delenv("TOKEN_SECRET", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("ALLOW_SESSION_SECRET_FALLBACK", raising=False)
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
    with pytest.raises(InvalidToken):
        decrypt(bad, secret=SECRET)
