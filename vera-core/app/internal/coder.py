"""Internal endpoints for vera-coder service:
  - GET /internal/coder/github-token: returns the GitHub PAT
  - GET /internal/coder/anthropic-key: returns active Anthropic key
  - POST /internal/coder/notify: receives PR-ready notifications, DMs Dima
"""
import hmac
import logging

from fastapi import APIRouter, Body, Header, HTTPException

from app.config import get_settings
from app.system.tools import _gh_token

log = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/coder")


def _require_secret(secret: str | None) -> None:
    expected = get_settings().internal_secret
    if not secret or not hmac.compare_digest(secret, expected):
        raise HTTPException(401, "invalid X-Internal-Secret")


@router.get("/github-token")
async def github_token(x_internal_secret: str | None = Header(default=None)) -> dict:
    _require_secret(x_internal_secret)
    tok = await _gh_token()
    if not tok:
        raise HTTPException(404, "no GitHub token configured")
    return {"token": tok}


@router.get("/anthropic-key")
async def anthropic_key(x_internal_secret: str | None = Header(default=None)) -> dict:
    _require_secret(x_internal_secret)
    from sqlalchemy import select
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import Token
    from vera_shared.crypto import decrypt, is_encrypted
    import os
    master = os.environ.get("SESSION_SECRET") or os.environ.get("TOKEN_ENC_KEY")
    async with get_session() as s:
        rs = (await s.execute(
            select(Token).where(Token.provider == "anthropic",
                                  Token.is_active == True)
            .order_by(Token.id).limit(1)
        )).scalars().all()
    if not rs:
        raise HTTPException(404, "no active anthropic token")
    t = rs[0]
    try:
        key = decrypt(t.token, master) if is_encrypted(t.token) else t.token
    except Exception:
        key = t.token
    return {"key": key}


@router.post("/notify")
async def notify(payload: dict = Body(...),
                  x_internal_secret: str | None = Header(default=None)) -> dict:
    _require_secret(x_internal_secret)
    from app.bot.sender import get_bot
    settings = get_settings()
    task = payload.get("task", "")
    result = payload.get("result") or {}
    ok = result.get("ok")
    pr_url = result.get("pr_url")
    summary = result.get("summary", "")
    branch = result.get("branch", "")
    err = result.get("error")

    lines = [f"🤖 <b>vera-coder</b> — {'✅ готово' if ok else '⚠️ не получилось'}"]
    lines.append(f"<i>Задача:</i> {task[:300]}")
    if branch:
        lines.append(f"<i>Ветка:</i> <code>{branch}</code>")
    if summary:
        lines.append(f"<i>Итог:</i> {summary[:400]}")
    if pr_url:
        lines.append(f"<b>PR:</b> {pr_url}")
    if err:
        lines.append(f"<i>Ошибка:</i> <code>{str(err)[:300]}</code>")

    try:
        await get_bot().send_message(
            chat_id=settings.owner_telegram_id,
            text="\n".join(lines), parse_mode="HTML",
        )
    except Exception as exc:
        log.warning("notify DM failed: %s", exc)
    return {"ok": True}
