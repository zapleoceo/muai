"""Instagram-логин — встроен в дашборд, тот же UX-паттерн что у gmail_oauth.

Аккаунт личный (не мультиарендный), прокси не нужен — сессия для него уже
успешно логинилась с этого же сервера в другом проекте, датацентр-IP сам по
себе не проблема. Флоу как в Stepan2 (app/api/_routes_channels.py):
логин по username/password → при 2FA/challenge показываем форму кода →
довершаем → сохраняем cl.get_settings() (encrypted) в instagram_sessions.

Пароль НИКОГДА не логируется и не сохраняется — живёт только в памяти
процесса на время флоу (until verify или до TTL), нужен исключительно чтобы
довершить 2FA повторным login(verification_code=...).

/api/instagram/start   (GET)  — owner-only, форма username/password
/api/instagram/start   (POST) — логин; при 2FA/challenge → форма кода
/api/instagram/verify  (POST) — код подтверждения → сохранить сессию
"""
from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from vera_shared.crypto import encrypt
from vera_shared.db.engine import get_session
from vera_shared.db.models_sources import InstagramSessionRow

from dashboard.auth import COOKIE_NAME, require_owner

log = logging.getLogger(__name__)
router = APIRouter()

# Флоу живут в памяти процесса (single-owner админка, рестарт дашборда —
# не проблема, просто начнёшь логин заново). TTL — не висеть вечно с паролем.
_FLOW_TTL_S = 600
_flows: dict[str, dict[str, Any]] = {}


def _prune_flows() -> None:
    now = time.monotonic()
    dead = [fid for fid, f in _flows.items() if now - f["ts"] > _FLOW_TTL_S]
    for fid in dead:
        _flows.pop(fid, None)


def _page(title: str, body_html: str, *, code: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>{title}</title><style>
body{{font-family:-apple-system,sans-serif;background:#0f1115;color:#e4e6eb;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.box{{background:#1a1d24;padding:40px;border-radius:16px;max-width:440px;width:100%}}
h1{{margin-top:0;font-size:20px}}
input{{width:100%;padding:10px 12px;margin:6px 0 14px;border-radius:8px;border:1px solid #333;
background:#0f1115;color:#e4e6eb;font-size:15px;box-sizing:border-box}}
button{{padding:10px 20px;border-radius:8px;border:none;background:#4dabf7;color:#fff;
font-weight:600;font-size:15px;cursor:pointer}}
label{{font-size:13px;color:#9aa0a8}}
a{{color:#4dabf7}} .err{{color:#ffaaaa}} .mute{{color:#9aa0a8;font-size:13px}}
</style></head><body><div class="box">{body_html}</div></body></html>""",
        status_code=code)


_FORM = """
<h1>📸 Подключить Instagram</h1>
<form method="post" action="/api/instagram/start">
  <label>Username</label><input name="username" required>
  <label>Password</label><input name="password" type="password" required>
  <button type="submit">Войти</button>
</form>
{error}
<p class="mute"><a href="/sources">← к источникам</a></p>
"""


@router.get("/api/instagram/start", response_class=HTMLResponse)
async def instagram_start_form(request: Request):
    require_owner(request, request.cookies.get(COOKIE_NAME))
    return _page("Instagram — вход", _FORM.format(error=""))


@router.post("/api/instagram/start", response_class=HTMLResponse)
async def instagram_start(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    require_owner(request, request.cookies.get(COOKIE_NAME))
    _prune_flows()

    from instagrapi import Client
    from instagrapi.exceptions import ChallengeRequired, TwoFactorRequired

    user, pw = username.strip(), password.strip()
    if not user or not pw:
        return _page("Instagram — вход",
                     _FORM.format(error='<p class="err">Заполни оба поля</p>'))

    cl = Client()
    cl.delay_range = [2, 5]
    try:
        await _run_in_thread(cl.login, user, pw)
    except TwoFactorRequired:
        fid = secrets.token_hex(8)
        _flows[fid] = {"client": cl, "kind": "2fa", "username": user,
                       "password": pw, "ts": time.monotonic()}
        return _page("Instagram — код подтверждения", _verify_form(fid))
    except ChallengeRequired:
        fid = secrets.token_hex(8)
        _flows[fid] = {"client": cl, "kind": "challenge", "ts": time.monotonic()}
        return _page("Instagram — код подтверждения", _verify_form(fid))
    except Exception as e:
        log.warning("instagram login failed for %s: %s", user, e)
        return _page("Instagram — вход",
                     _FORM.format(error=f'<p class="err">Ошибка: {_esc(str(e)[:200])}</p>'))

    await _save_session(user, cl)
    return _done_page(user)


def _verify_form(flow_id: str) -> str:
    return f"""
<h1>Instagram — код подтверждения</h1>
<p class="mute">Instagram прислал код (SMS/email/приложение) или запросил 2FA.</p>
<form method="post" action="/api/instagram/verify">
  <input type="hidden" name="flow_id" value="{flow_id}">
  <label>Код</label><input name="code" required autofocus>
  <button type="submit">Подтвердить</button>
</form>
"""


@router.post("/api/instagram/verify", response_class=HTMLResponse)
async def instagram_verify(
    request: Request,
    flow_id: str = Form(...),
    code: str = Form(...),
):
    require_owner(request, request.cookies.get(COOKIE_NAME))
    _prune_flows()
    flow = _flows.pop(flow_id, None)
    if flow is None:
        return _page("Ошибка", "<h1 class='err'>Флоу истёк, начни заново</h1>"
                     "<p><a href='/api/instagram/start'>← начать заново</a></p>", code=400)

    cl = flow["client"]
    code = code.strip()
    try:
        if flow["kind"] == "challenge":
            cl.challenge_code_handler = lambda _u, _c: code
            await _run_in_thread(cl.challenge_resolve, cl.last_json)
        else:
            await _run_in_thread(cl.login, flow["username"], flow["password"],
                                 verification_code=code)
    except Exception as e:
        log.warning("instagram verify failed: %s", e)
        return _page("Instagram — код подтверждения",
                     _verify_form(flow_id) + f'<p class="err">Ошибка: {_esc(str(e)[:200])}</p>')

    username = flow.get("username") or getattr(cl, "username", "") or "unknown"
    await _save_session(username, cl)
    return _done_page(username)


async def _save_session(username: str, cl: Any) -> None:
    settings_json = cl.get_settings()
    import json
    enc = encrypt(json.dumps(settings_json))
    async with get_session() as s:
        existing = (await s.execute(
            select(InstagramSessionRow).where(InstagramSessionRow.username == username)
        )).scalar_one_or_none()
        if existing:
            existing.session_json_enc = enc
            existing.is_active = True
            existing.last_polled_at = None
        else:
            s.add(InstagramSessionRow(username=username, session_json_enc=enc,
                                      is_active=True))
    log.info("Instagram сессия сохранена: @%s", username)


def _done_page(username: str) -> HTMLResponse:
    return _page("Готово", f"<h1>✓</h1><p>Instagram подключён: "
                 f"<b>@{_esc(username)}</b></p>"
                 "<p class='mute'>Ингестор подхватит сессию сам "
                 "(проверка раз в 10 мин, если ждал — иначе следующий poll).</p>"
                 "<p><a href='/sources'>← к источникам</a></p>")


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


async def _run_in_thread(fn, *args, **kwargs):
    import asyncio
    return await asyncio.to_thread(fn, *args, **kwargs)
