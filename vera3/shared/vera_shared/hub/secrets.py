"""Секреты хаба: шифрование секретов апстримов и токены агентов.

Ключ отдельный — HUB_SECRET_KEY, не общий TOKEN_SECRET: его получают только
дашборд (шифрует при сохранении) и mcp-hub (расшифровывает перед вызовом).
Утечка ключа любого другого сервиса не открывает секреты апстримов.
"""
from __future__ import annotations

import hashlib
import os
import secrets

from vera_shared import crypto

AGENT_TOKEN_PREFIX = "vhub_"
_MIN_KEY_LEN = 32


class HubKeyMissing(RuntimeError):
    """HUB_SECRET_KEY не задан или короче 32 символов."""


def hub_key() -> str:
    key = os.environ.get("HUB_SECRET_KEY", "").strip()
    if len(key) < _MIN_KEY_LEN:
        raise HubKeyMissing("HUB_SECRET_KEY is not set (>=32 chars, `openssl rand -hex 32`)")
    return key


def seal(plain: str) -> str:
    return crypto.encrypt(plain, secret=hub_key())


def unseal(stored: str) -> str:
    return crypto.decrypt(stored, secret=hub_key())


def new_agent_token() -> str:
    return AGENT_TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
