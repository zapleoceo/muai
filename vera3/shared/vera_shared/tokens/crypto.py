"""Token encryption at rest — Fernet AES."""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet


ENCRYPTED_PREFIX = "enc1:"  # version tag для будущих миграций алгоритма


def _fernet_from_secret(secret: str) -> Fernet:
    """Derive Fernet key from arbitrary secret string."""
    if not secret:
        raise ValueError("Empty TOKEN_SECRET — refusing to use weak encryption")
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def token_secret() -> str:
    """Read TOKEN_SECRET (or fallback SESSION_SECRET) from env."""
    return (
        os.environ.get("TOKEN_SECRET")
        or os.environ.get("SESSION_SECRET")
        or ""
    )


def encrypt(plain: str, secret: str | None = None) -> str:
    """Encrypt string token. Output format: enc1:<base64>."""
    secret = secret or token_secret()
    if not secret:
        raise ValueError("No TOKEN_SECRET available")
    f = _fernet_from_secret(secret)
    encrypted = f.encrypt(plain.encode("utf-8")).decode("ascii")
    return f"{ENCRYPTED_PREFIX}{encrypted}"


def decrypt(stored: str, secret: str | None = None) -> str:
    """Decrypt stored token. Bypass для plaintext (миграция legacy данных)."""
    if not stored.startswith(ENCRYPTED_PREFIX):
        # Legacy plaintext token (e.g. from Vera 2.0 migration before re-encrypt)
        return stored
    secret = secret or token_secret()
    if not secret:
        raise ValueError("No TOKEN_SECRET available")
    f = _fernet_from_secret(secret)
    payload = stored[len(ENCRYPTED_PREFIX):]
    return f.decrypt(payload.encode("ascii")).decode("utf-8")


def is_encrypted(stored: str) -> bool:
    return stored.startswith(ENCRYPTED_PREFIX)
