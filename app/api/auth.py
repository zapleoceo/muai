import base64
import hashlib
import hmac
import json
import time

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import get_settings

router = APIRouter()
settings = get_settings()

OWNER_ID = 169510539
SESSION_TTL = 86400 * 30  # 30 days


def _make_token(user_id: int) -> str:
    payload = json.dumps({"uid": user_id, "exp": int(time.time()) + SESSION_TTL})
    sig = hmac.new(settings.session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def _verify_token(token: str) -> int | None:
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        payload, sig = decoded.rsplit("|", 1)
        expected = hmac.new(settings.session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(payload)
        if data["exp"] < time.time():
            return None
        return data["uid"]
    except Exception:
        return None


def _verify_telegram_data(data: dict) -> bool:
    received_hash = data.pop("hash", "")
    check_arr = sorted(f"{k}={v}" for k, v in data.items() if v)
    check_string = "\n".join(check_arr)
    secret_key = hashlib.sha256(settings.telegram_bot_token.encode()).digest()
    computed = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, received_hash)


def require_owner(session: str | None = Cookie(default=None)) -> int:
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    uid = _verify_token(session)
    if uid != OWNER_ID:
        raise HTTPException(status_code=403, detail="Forbidden")
    return uid


@router.post("/auth/telegram")
async def telegram_auth(request: Request) -> JSONResponse:
    data = dict(await request.json())

    if not _verify_telegram_data(data):
        raise HTTPException(status_code=403, detail="Invalid Telegram auth data")

    user_id = int(data.get("id", 0))
    if user_id != OWNER_ID:
        raise HTTPException(status_code=403, detail="Access denied")

    token = _make_token(user_id)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("session", token, httponly=True, secure=True, samesite="lax", max_age=SESSION_TTL)
    return resp


@router.get("/auth/logout")
async def logout() -> RedirectResponse:
    resp = RedirectResponse("/")
    resp.delete_cookie("session")
    return resp
