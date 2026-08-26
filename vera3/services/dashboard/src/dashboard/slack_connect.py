"""Подключение Slack из дашборда — токен вводится здесь, а не правкой .env.

Токен проверяется ДО сохранения: `auth.test` подтверждает, что он живой, и
заодно отдаёт воркспейс и имя владельца — иначе опечатка молча легла бы в БД,
а поллер раз в десять минут писал бы «нет доступа» в лог контейнера.

Токен не логируется никогда и в HTML не возвращается. В БД идёт зашифрованным
(`crypto.encrypt`), как сессии telegram и instagram.

/api/slack/start  (GET)  — owner-only, форма с инструкцией
/api/slack/start  (POST) — проверить, сохранить, вернуть на страницу источника
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from vera_shared.crypto import encrypt
from vera_shared.db.engine import get_session
from vera_shared.db.models_sources import SlackAuthRow

from dashboard.auth import COOKIE_NAME, require_owner
from dashboard.stats import drop_detail_cache

log = logging.getLogger(__name__)
router = APIRouter()

AUTH_TEST_URL = "https://slack.com/api/auth.test"
#: чего не хватает чаще всего — переводим код Slack в понятную причину.
_REASONS = {
    "invalid_auth": "токен не принят — проверь, что скопирован целиком",
    "not_authed": "токен пустой или не тот",
    "token_revoked": "токен отозван в Slack",
    "token_expired": "срок действия токена истёк",
    "account_inactive": "аккаунт в этом воркспейсе отключён",
    "missing_scope": "у токена не хватает прав — переустанови приложение",
}


async def verify(token: str) -> tuple[dict, str | None]:
    """→ (ответ auth.test, причина отказа). Причина None — токен живой."""
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(AUTH_TEST_URL,
                             headers={"Authorization": f"Bearer {token}"})
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return {}, f"Slack не ответил: {type(e).__name__}"
    if data.get("ok"):
        return data, None
    code = str(data.get("error") or "unknown")
    return {}, _REASONS.get(code, f"Slack ответил: {code}")


async def save_token(token: str, who: dict) -> None:
    """Upsert по воркспейсу: повторное подключение обновляет строку, а не
    плодит вторую. Курсоры каналов при этом целы — история не поедет заново."""
    team_id = str(who.get("team_id") or "unknown")
    async with get_session() as s:
        row = (await s.execute(
            select(SlackAuthRow).where(SlackAuthRow.team_id == team_id)
        )).scalar_one_or_none()
        if row is None:
            row = SlackAuthRow(team_id=team_id, token_enc=encrypt(token))
            s.add(row)
        else:
            row.token_enc = encrypt(token)
        row.team_name = str(who.get("team") or "")[:255]
        row.user_id = str(who.get("user_id") or "")
        row.username = str(who.get("user") or "")[:255]
        row.is_active = True
        row.last_error = None
    drop_detail_cache("slack")


def _page(body: str, *, code: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Подключить Slack</title><style>
body{{font-family:-apple-system,sans-serif;background:#0f1115;color:#e4e6eb;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:24px}}
.box{{background:#1a1d24;padding:36px;border-radius:16px;max-width:520px;width:100%}}
h1{{margin:0 0 6px;font-size:20px}}
input{{width:100%;padding:11px 12px;margin:8px 0 16px;border-radius:8px;
border:1px solid #2a2d34;background:#0f1115;color:#e4e6eb;font-size:14px;
font-family:'SF Mono',Monaco,monospace;box-sizing:border-box}}
button{{padding:11px 22px;border-radius:8px;border:none;background:#4dabf7;color:#fff;
font-weight:600;font-size:15px;cursor:pointer}}
label{{font-size:13px;color:#9aa0a8}}
a{{color:#4dabf7;text-decoration:none}}
.err{{background:#4a1a1d;color:#ffaaaa;padding:12px 14px;border-radius:8px;
margin:0 0 16px;font-size:14px}}
.mute{{color:#6b7280;font-size:13px;line-height:1.55}}
ol{{color:#9aa0a8;font-size:13px;line-height:1.7;padding-left:20px;margin:14px 0 20px}}
code{{background:#0f1115;padding:1px 5px;border-radius:4px;font-size:12px}}
</style></head><body><div class="box">{body}</div></body></html>""", status_code=code)


_FORM = """
<h1>💬 Подключить Slack</h1>
<p class="mute">Нужен <b>пользовательский</b> токен (<code>xoxp-</code>), не бот:
Вера должна видеть то, что видишь ты, включая личку и приватные каналы.</p>
<ol>
  <li><a href="https://api.slack.com/apps" target="_blank" rel="noopener">api.slack.com/apps</a>
      → приложение своего воркспейса. Публиковать его не надо: у внутренних
      приложений лимит запросов в разы выше.</li>
  <li><b>OAuth &amp; Permissions</b> → <b>User Token Scopes</b>:
      <code>channels:history</code> <code>channels:read</code>
      <code>groups:history</code> <code>groups:read</code>
      <code>im:history</code> <code>im:read</code>
      <code>mpim:history</code> <code>mpim:read</code>
      <code>users:read</code> <code>users:read.email</code>
      <code>reactions:read</code> <code>files:read</code></li>
  <li><b>Install to Workspace</b> → скопируй <b>User OAuth Token</b>.</li>
</ol>
{error}
<form method="post" action="/api/slack/start">
  <label>User OAuth Token</label>
  <input name="token" type="password" required autocomplete="off"
         placeholder="xoxp-…" spellcheck="false">
  <button type="submit">Проверить и подключить</button>
</form>
<p class="mute" style="margin-top:18px">Токен проверяется до сохранения и
хранится в базе зашифрованным. <a href="/sources/slack">← к источнику</a></p>
"""


@router.get("/api/slack/start", response_class=HTMLResponse)
async def slack_start_form(request: Request):
    require_owner(request, request.cookies.get(COOKIE_NAME))
    return _page(_FORM.format(error=""))


@router.post("/api/slack/start")
async def slack_start(request: Request, token: str = Form(...)):
    require_owner(request, request.cookies.get(COOKIE_NAME))
    token = token.strip()

    who, reason = await verify(token)
    if reason is not None:
        # Причина отказа в HTML — да, сам токен — никогда.
        log.warning("slack connect отклонён: %s", reason)
        return _page(_FORM.format(error=f'<div class="err">{reason}</div>'), code=400)

    await save_token(token, who)
    log.info("slack подключён: %s / %s", who.get("team"), who.get("user"))
    return RedirectResponse("/sources/slack", status_code=303)
