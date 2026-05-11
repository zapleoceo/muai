import base64
import hashlib
import hmac
import json
import time

from app.config import get_settings

SESSION_TTL = 86400 * 30  # 30 days


def make_token(user_id: int) -> str:
    secret = get_settings().session_secret.encode()
    payload = json.dumps({"uid": user_id, "exp": int(time.time()) + SESSION_TTL})
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def verify_token(token: str) -> int | None:
    secret = get_settings().session_secret.encode()
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        payload, sig = decoded.rsplit("|", 1)
        expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(payload)
        if data["exp"] < time.time():
            return None
        return data["uid"]
    except Exception:
        return None


def verify_telegram_widget(data: dict) -> bool:
    """Validate Telegram Login Widget payload using HMAC-SHA256(bot_token)."""
    bot_token = get_settings().telegram_bot_token.encode()
    received_hash = data.pop("hash", "")
    check_string = "\n".join(sorted(f"{k}={v}" for k, v in data.items() if v))
    secret_key = hashlib.sha256(bot_token).digest()
    computed = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, received_hash)
