import hmac
import hashlib
import time
from urllib.parse import quote

from fastapi import Cookie, HTTPException, status

from app.config import get_settings

_COOKIE = "vera_session"
_TTL = 60 * 60 * 24 * 30  # 30 days


def _sign(payload: str, secret: str) -> str:
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify(token: str, secret: str) -> str | None:
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        issued = int(payload.split(":")[1])
    except Exception:
        return None
    if time.time() - issued > _TTL:
        return None
    return payload


def issue_session() -> tuple[str, int]:
    settings = get_settings()
    payload = f"owner:{int(time.time())}"
    return _sign(payload, settings.session_secret), _TTL


def require_owner(vera_session: str | None = Cookie(default=None)) -> bool:
    settings = get_settings()
    if not vera_session or _verify(vera_session, settings.session_secret) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized")
    return True


def check_deploy_secret(token: str) -> bool:
    settings = get_settings()
    return hmac.compare_digest(token, settings.deploy_secret)


def verify_telegram_auth(data: dict) -> int | None:
    """Verify Telegram Login Widget payload. Returns user_id if valid, else None."""
    settings = get_settings()
    received_hash = data.get("hash")
    if not received_hash:
        return None
    fields = {k: v for k, v in data.items() if k != "hash"}
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hashlib.sha256(settings.telegram_bot_token_vera.encode()).digest()
    computed = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        return None
    try:
        auth_date = int(fields.get("auth_date", "0"))
        if time.time() - auth_date > 86400:  # 24h window
            return None
        return int(fields["id"])
    except (KeyError, ValueError):
        return None
