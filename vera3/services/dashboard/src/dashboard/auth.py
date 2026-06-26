"""Telegram Login Widget auth — порт из Vera 2.

Cookie payload теперь привязан к OWNER_ID — смена OWNER_TELEGRAM_ID
инвалидирует все ранее выпущенные сессии.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

from fastapi import Cookie, HTTPException, Request

SESSION_SECRET = os.environ["TOKEN_SECRET"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_TELEGRAM_ID"])
if OWNER_ID == 0:
    raise RuntimeError("OWNER_TELEGRAM_ID не должен быть 0 в проде")
COOKIE_NAME = "vera3_session"
TTL = 60 * 60 * 24 * 30  # 30 days
# Telegram widget auth_date — окно 5 минут (раньше было 24 часа).
WIDGET_AUTH_TTL_S = 300


def _sign(payload: str) -> str:
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify(token: str | None) -> bool:
    """HMAC + срок + binding к OWNER_ID."""
    if not token or "." not in token:
        return False
    payload, sig = token.rsplit(".", 1)
    expected = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    # payload = "owner:<owner_id>:<issued_ts>"
    parts = payload.split(":")
    if len(parts) != 3 or parts[0] != "owner":
        return False
    try:
        owner_in_token = int(parts[1])
        issued = int(parts[2])
    except ValueError:
        return False
    if owner_in_token != OWNER_ID:
        return False
    return time.time() - issued <= TTL


def issue_session() -> tuple[str, int]:
    return _sign(f"owner:{OWNER_ID}:{int(time.time())}"), TTL


def verify_telegram_auth(data: dict) -> int | None:
    """Проверяет подпись от Telegram Login Widget.

    Возвращает user_id если подпись валидна и не старше WIDGET_AUTH_TTL_S.
    """
    received_hash = data.get("hash")
    if not received_hash:
        return None
    fields = {k: v for k, v in data.items() if k != "hash"}
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hashlib.sha256(BOT_TOKEN.encode()).digest()
    computed = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        return None
    try:
        auth_date = int(fields.get("auth_date", "0"))
        if time.time() - auth_date > WIDGET_AUTH_TTL_S:
            return None
        return int(fields["id"])
    except (KeyError, ValueError):
        return None


def require_owner(request: Request,
                  vera3_session: str | None = Cookie(default=None)) -> None:
    if not vera3_session or not _verify(vera3_session):
        raise HTTPException(401, "unauthorized")


def get_bot_username() -> str:
    return os.environ.get("TELEGRAM_BOT_USERNAME", "Dimondra_Ai_Bot")


# ─── OAuth state (anti-CSRF для Gmail re-auth) ───────────────────────────────
# Stateless: подписываем timestamp тем же секретом. Не зависит от памяти
# процесса (переживает рестарт dashboard, работает при нескольких воркерах).
OAUTH_STATE_TTL_S = 600  # 10 минут на прохождение consent


def issue_oauth_state() -> str:
    return _sign(f"gmailoauth:{int(time.time())}")


def verify_oauth_state(state: str | None) -> bool:
    if not state or "." not in state:
        return False
    payload, sig = state.rsplit(".", 1)
    expected = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    parts = payload.split(":")
    if len(parts) != 2 or parts[0] != "gmailoauth":
        return False
    try:
        issued = int(parts[1])
    except ValueError:
        return False
    return time.time() - issued <= OAUTH_STATE_TTL_S
