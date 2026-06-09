"""Token encryption at rest — Fernet AES.

SECURITY: plaintext-bypass в `decrypt()` (для legacy миграции Vera 2) теперь
гейтится env флагом `ALLOW_PLAINTEXT_TOKENS=1`. Без флага — отказ.
В проде флаг НЕ ставится. Используется только для одноразовой миграции.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken


log = logging.getLogger(__name__)

ENCRYPTED_PREFIX = "enc1:"  # version tag для будущих миграций алгоритма


def _fernet_from_secret(secret: str) -> Fernet:
    """Derive Fernet key from arbitrary secret string."""
    if not secret:
        raise ValueError("Empty TOKEN_SECRET — refusing to use weak encryption")
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def token_secret() -> str:
    """Read TOKEN_SECRET from env. SESSION_SECRET — НЕ fallback в проде."""
    secret = os.environ.get("TOKEN_SECRET", "").strip()
    if secret:
        return secret
    # Fallback на SESSION_SECRET — только если явно разрешено (для dev / тестов)
    if os.environ.get("ALLOW_SESSION_SECRET_FALLBACK") == "1":
        return os.environ.get("SESSION_SECRET", "")
    return ""


def encrypt(plain: str, secret: str | None = None) -> str:
    """Encrypt string token. Output format: enc1:<base64>."""
    secret = secret or token_secret()
    if not secret:
        raise ValueError("No TOKEN_SECRET available")
    f = _fernet_from_secret(secret)
    encrypted = f.encrypt(plain.encode("utf-8")).decode("ascii")
    return f"{ENCRYPTED_PREFIX}{encrypted}"


def decrypt(stored: str, secret: str | None = None) -> str:
    """Decrypt stored token.

    Plaintext-bypass гейтится env `ALLOW_PLAINTEXT_TOKENS=1`. Без флага — отказ.
    Это закрывает вектор "атакующий с DB write-access подсовывает свой sk-..."
    """
    if not stored.startswith(ENCRYPTED_PREFIX):
        if os.environ.get("ALLOW_PLAINTEXT_TOKENS") == "1":
            log.warning(
                "Plaintext token in DB (legacy bypass active) — len=%d",
                len(stored),
            )
            return stored
        raise InvalidToken(
            "Token in DB не зашифрован (нет префикса enc1:). "
            "Если это legacy миграция — выставь ALLOW_PLAINTEXT_TOKENS=1, "
            "иначе шифр совершенно сломан или таблица скомпрометирована."
        )
    secret = secret or token_secret()
    if not secret:
        raise ValueError("No TOKEN_SECRET available")
    f = _fernet_from_secret(secret)
    payload = stored[len(ENCRYPTED_PREFIX):]
    return f.decrypt(payload.encode("ascii")).decode("utf-8")


def is_encrypted(stored: str) -> bool:
    return stored.startswith(ENCRYPTED_PREFIX)
