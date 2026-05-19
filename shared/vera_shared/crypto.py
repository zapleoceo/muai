"""Stdlib-only symmetric encryption for at-rest secrets.

Uses HMAC-SHA256 derived key over the master secret + per-message random IV
and AES-256-CTR via the `cryptography` package which is already a transitive
dep (httpx → certifi → not directly, but pyca-cryptography is provided by
voyageai/anthropic deps). We import lazily so module load works without it.

Cipher format: base64( iv[16] || ciphertext )
Plaintext rows kept readable for backward compat: decrypt() returns input
unchanged if it doesn't have the magic prefix.
"""
import base64
import hashlib
import hmac
import os

_PREFIX = "enc1:"


def _derive_key(master: str) -> bytes:
    return hashlib.sha256(b"vera-token-enc-v1|" + master.encode()).digest()


def encrypt(plaintext: str, master_secret: str) -> str:
    if not plaintext:
        return plaintext
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    iv = os.urandom(16)
    key = _derive_key(master_secret)
    encryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    ct = encryptor.update(plaintext.encode("utf-8")) + encryptor.finalize()
    mac = hmac.new(key, iv + ct, hashlib.sha256).digest()[:16]
    blob = base64.urlsafe_b64encode(iv + mac + ct).decode("ascii")
    return _PREFIX + blob


def decrypt(stored: str, master_secret: str) -> str:
    if not stored or not stored.startswith(_PREFIX):
        return stored
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    raw = base64.urlsafe_b64decode(stored[len(_PREFIX):].encode("ascii"))
    iv, mac, ct = raw[:16], raw[16:32], raw[32:]
    key = _derive_key(master_secret)
    expected = hmac.new(key, iv + ct, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(mac, expected):
        raise ValueError("token decrypt: hmac mismatch (wrong secret or tampered)")
    decryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
    pt = decryptor.update(ct) + decryptor.finalize()
    return pt.decode("utf-8")


def is_encrypted(stored: str) -> bool:
    return bool(stored) and stored.startswith(_PREFIX)
