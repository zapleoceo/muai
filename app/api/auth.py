from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import get_settings
from app.services.auth import SESSION_TTL, make_token, verify_telegram_widget, verify_token

router = APIRouter()


def require_owner(session: str | None = Cookie(default=None)) -> int:
    uid = verify_token(session or "")
    if uid != get_settings().owner_telegram_id:
        raise HTTPException(status_code=401 if not session else 403)
    return uid


@router.post("/auth/telegram")
async def telegram_auth(request: Request) -> JSONResponse:
    data = dict(await request.json())
    if not verify_telegram_widget(data):
        raise HTTPException(status_code=403, detail="Invalid Telegram auth data")

    user_id = int(data.get("id", 0))
    if user_id != get_settings().owner_telegram_id:
        raise HTTPException(status_code=403, detail="Access denied")

    resp = JSONResponse({"ok": True})
    resp.set_cookie("session", make_token(user_id), httponly=True, secure=True,
                    samesite="lax", max_age=SESSION_TTL)
    return resp


@router.get("/auth/logout")
async def logout() -> RedirectResponse:
    resp = RedirectResponse("/")
    resp.delete_cookie("session")
    return resp
