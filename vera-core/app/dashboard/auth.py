import hashlib
import hmac
import time

from fastapi import Cookie, Header, HTTPException, Request, status

from app.config import get_settings

_COOKIE = "vera_session"
_TTL = 60 * 60 * 24 * 7  # 7 days
_CSRF_TTL = 60 * 60 * 24  # 1 day
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


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


def issue_csrf(session_token: str) -> str:
    """Stateless CSRF token derived from the session token. Anyone with a
    valid session cookie can request one; only same-origin requests can
    read it back via /api/csrf and resend it as X-CSRF header."""
    settings = get_settings()
    return hmac.new(settings.session_secret.encode(),
                    f"csrf:{session_token}".encode(),
                    hashlib.sha256).hexdigest()


def _csrf_valid(session_token: str, presented: str | None) -> bool:
    if not presented:
        return False
    expected = issue_csrf(session_token)
    return hmac.compare_digest(presented, expected)


def require_owner(request: Request,
                  vera_session: str | None = Cookie(default=None),
                  x_csrf: str | None = Header(default=None)) -> bool:
    settings = get_settings()
    if not vera_session or _verify(vera_session, settings.session_secret) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized")
    if request.method in _MUTATING_METHODS and not _csrf_valid(vera_session, x_csrf):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid CSRF token")
    return True


def verify_telegram_auth(data: dict) -> int | None:
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
        if time.time() - auth_date > 86400:
            return None
        return int(fields["id"])
    except (KeyError, ValueError):
        return None
