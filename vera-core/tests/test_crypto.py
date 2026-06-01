"""Token encryption — guarantees plaintext keys are never persisted."""
import pytest

from vera_shared.crypto import decrypt, encrypt, is_encrypted


_SECRET = "test-master-key-32-bytes-min-padddddd"


def test_roundtrip_preserves_plaintext():
    plain = "AIzaSyA-FAKE-key"
    cipher = encrypt(plain, _SECRET)
    assert decrypt(cipher, _SECRET) == plain


def test_encrypted_marker_is_recognisable():
    cipher = encrypt("anything", _SECRET)
    assert is_encrypted(cipher) is True
    assert is_encrypted("plain-text") is False
    assert is_encrypted("") is False


def test_decrypt_with_wrong_secret_fails():
    cipher = encrypt("payload", _SECRET)
    with pytest.raises(Exception):
        decrypt(cipher, "other-master-key-not-the-same-one-pad")


def test_cipher_does_not_contain_plaintext():
    plain = "AIzaSyA-VERY-DISTINCTIVE-FAKE"
    cipher = encrypt(plain, _SECRET)
    assert plain not in cipher
