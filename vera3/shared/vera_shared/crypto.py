"""Secret encryption at rest — Fernet AES.

Used to encrypt session-style secrets in the DB: Gmail OAuth refresh
tokens, Instagram sessionid, Telegram userbot session strings. These are
NOT LLM provider keys — Vera holds no LLM keys; all LLM calls go through
the broker. This module is the only crypto helper Vera needs.

SECURITY: plaintext-bypass in `decrypt()` (for legacy Vera-2 migration) is
gated by env flag `ALLOW_PLAINTEXT_TOKENS=1`. Without the flag — refusal.
In prod the flag is NOT set; it exists only for one-off migration.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

ENCRYPTED_PREFIX = "enc1:"  # version tag for future algorithm migrations


def _fernet_from_secret(secret: str) -> Fernet:
    """Derive a Fernet key from an arbitrary secret string."""
    if not secret:
        raise ValueError("Empty TOKEN_SECRET — refusing to use weak encryption")
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def token_secret() -> str:
    """Read TOKEN_SECRET from env. SESSION_SECRET is NOT a prod fallback."""
    secret = os.environ.get("TOKEN_SECRET", "").strip()
    if secret:
        return secret
    if os.environ.get("ALLOW_SESSION_SECRET_FALLBACK") == "1":
        return os.environ.get("SESSION_SECRET", "")
    return ""


def encrypt(plain: str, secret: str | None = None) -> str:
    """Encrypt a secret. Output format: enc1:<base64>."""
    secret = secret or token_secret()
    if not secret:
        raise ValueError("No TOKEN_SECRET available")
    f = _fernet_from_secret(secret)
    encrypted = f.encrypt(plain.encode("utf-8")).decode("ascii")
    return f"{ENCRYPTED_PREFIX}{encrypted}"


def decrypt(stored: str, secret: str | None = None) -> str:
    """Decrypt a stored secret.

    Plaintext-bypass gated by env `ALLOW_PLAINTEXT_TOKENS=1`. Without the
    flag — refusal. Closes the "attacker with DB write-access drops in a
    plaintext value" vector."""
    if not stored.startswith(ENCRYPTED_PREFIX):
        if os.environ.get("ALLOW_PLAINTEXT_TOKENS") == "1":
            log.warning("Plaintext secret in DB (legacy bypass active) — len=%d",
                        len(stored))
            return stored
        raise InvalidToken(
            "Stored secret is not encrypted (no enc1: prefix). For a legacy "
            "migration set ALLOW_PLAINTEXT_TOKENS=1, otherwise the cipher is "
            "broken or the row was tampered with."
        )
    secret = secret or token_secret()
    if not secret:
        raise ValueError("No TOKEN_SECRET available")
    f = _fernet_from_secret(secret)
    payload = stored[len(ENCRYPTED_PREFIX):]
    return f.decrypt(payload.encode("ascii")).decode("utf-8")


def is_encrypted(stored: str) -> bool:
    return stored.startswith(ENCRYPTED_PREFIX)
