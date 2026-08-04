"""Telegram userbot re-auth — встроен в дашборд, тот же UX что у instagram_login.

Сессия Telethon иногда отзывается (AuthKeyUnregistered) — тогда ингестор
уходит в рестарт-луп. Раньше чинилось только CLI-скриптом на сервере; теперь
owner может перелогиниться кнопкой в /sources.

Флоу: номер → SMS-код → (если включён облачный 2FA) пароль → сохраняем новую
StringSession (encrypted) в telegram_sessions, деактивируя старые для номера.
Ингестор подхватит её сам на следующем рестарте (он и так циклится).

Облачный 2FA-пароль НИКОГДА не логируется и не сохраняется — живёт только в
POST-запросе, нужен один раз чтобы довершить sign_in(password=...).

/api/telegram/start  (GET)  — owner-only, форма номера (префилл из env)
/api/telegram/start  (POST) — шлём код, показываем форму кода
/api/telegram/verify (POST) — код (+ пароль при 2FA) → сохранить сессию
"""
from __future__ import annotations

import contextlib
import logging
import os
import secrets
import time
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from vera_shared.crypto import encrypt
from vera_shared.db.engine import get_session
from vera_shared.db.models_sources import TelegramSessionRow

from dashboard.auth import COOKIE_NAME, require_owner

log = logging.getLogger(__name__)
router = APIRouter()

_FLOW_TTL_S = 600
_flows: dict[str, dict[str, Any]] = {}


async def _prune_flows() -> None:
    now = time.monotonic()
    for fid, f in list(_flows.items()):
        if now - f["ts"] > _FLOW_TTL_S:
            client = f.get("client")
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.disconnect()
            _flows.pop(fid, None)


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


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


def _phone_form(error: str = "") -> str:
    phone = _esc(os.environ.get("TELEGRAM_PHONE", ""))
    return f"""
<h1>✈️ Переподключить Telegram</h1>
<p class="mute">Telegram пришлёт код в приложение (или SMS).</p>
<form method="post" action="/api/telegram/start">
  <label>Номер телефона</label><input name="phone" value="{phone}" required>
  <button type="submit">Выслать код</button>
</form>
{error}
<p class="mute"><a href="/sources">← к источникам</a></p>
"""


def _code_form(flow_id: str, error: str = "") -> str:
    return f"""
<h1>✈️ Telegram — код из приложения</h1>
<p class="mute">Введи 5-значный код. Если включён облачный пароль (2FA) —
попросим его следующим шагом.</p>
<form method="post" action="/api/telegram/verify">
  <input type="hidden" name="flow_id" value="{flow_id}">
  <label>Код</label><input name="code" inputmode="numeric" autofocus required>
  <button type="submit">Подтвердить</button>
</form>
{error}
"""


def _password_form(flow_id: str, error: str = "") -> str:
    return f"""
<h1>✈️ Telegram — облачный пароль (2FA)</h1>
<p class="mute">У аккаунта включена двухэтапная проверка. Введи облачный пароль
Telegram (не код из SMS).</p>
<form method="post" action="/api/telegram/verify">
  <input type="hidden" name="flow_id" value="{flow_id}">
  <label>Облачный пароль</label><input name="password" type="password" autofocus required>
  <button type="submit">Войти</button>
</form>
{error}
"""


@router.get("/api/telegram/start", response_class=HTMLResponse)
async def telegram_start_form(request: Request):
    require_owner(request, request.cookies.get(COOKIE_NAME))
    return _page("Telegram — вход", _phone_form())


@router.post("/api/telegram/start", response_class=HTMLResponse)
async def telegram_start(request: Request, phone: str = Form(...)):
    require_owner(request, request.cookies.get(COOKIE_NAME))
    await _prune_flows()

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    phone = phone.strip() or os.environ.get("TELEGRAM_PHONE", "")
    if not phone:
        return _page("Telegram — вход",
                     _phone_form('<p class="err">Укажи номер</p>'))

    client = TelegramClient(StringSession(), int(os.environ["TELEGRAM_API_ID"]),
                            os.environ["TELEGRAM_API_HASH"],
                            device_model="Vera 3.0 ingestor",
                            system_version="docker", app_version="3.0")
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
    except Exception as e:
        await client.disconnect()
        log.warning("telegram send_code failed for %s: %s", phone, e)
        return _page("Telegram — вход",
                     _phone_form(f'<p class="err">Ошибка: {_esc(str(e)[:200])}</p>'))

    fid = secrets.token_hex(8)
    _flows[fid] = {"client": client, "phone": phone,
                   "hash": sent.phone_code_hash, "await_pwd": False,
                   "ts": time.monotonic()}
    return _page("Telegram — код", _code_form(fid))


@router.post("/api/telegram/verify", response_class=HTMLResponse)
async def telegram_verify(
    request: Request,
    flow_id: str = Form(...),
    code: str = Form(""),
    password: str = Form(""),
):
    require_owner(request, request.cookies.get(COOKIE_NAME))
    await _prune_flows()
    flow = _flows.get(flow_id)
    if flow is None:
        return _page("Ошибка", "<h1 class='err'>Флоу истёк, начни заново</h1>"
                     "<p><a href='/api/telegram/start'>← начать заново</a></p>", code=400)

    from telethon.errors import SessionPasswordNeededError

    client = flow["client"]
    flow["ts"] = time.monotonic()
    try:
        if flow["await_pwd"]:
            if not password.strip():
                return _page("Telegram — 2FA", _password_form(
                    flow_id, '<p class="err">Введи облачный пароль</p>'))
            await client.sign_in(password=password.strip())
        else:
            await client.sign_in(flow["phone"], code.strip(),
                                 phone_code_hash=flow["hash"])
    except SessionPasswordNeededError:
        flow["await_pwd"] = True
        return _page("Telegram — 2FA", _password_form(flow_id))
    except Exception as e:
        log.warning("telegram sign_in failed: %s", e)
        form = _password_form if flow["await_pwd"] else _code_form
        return _page("Telegram — вход",
                     form(flow_id, f'<p class="err">Ошибка: {_esc(str(e)[:200])}</p>'))

    from telethon.sessions import StringSession
    session_str = StringSession.save(client.session)
    await client.disconnect()
    _flows.pop(flow_id, None)
    await _save_session(flow["phone"], session_str)
    return _done_page(flow["phone"])


async def _save_session(phone: str, session_str: str) -> None:
    enc = encrypt(session_str)
    async with get_session() as s:
        rows = (await s.execute(
            select(TelegramSessionRow).where(TelegramSessionRow.phone == phone)
        )).scalars().all()
        if rows:
            for r in rows:
                r.is_active = False
            rows[0].session_string_enc = enc
            rows[0].is_active = True
        else:
            s.add(TelegramSessionRow(phone=phone, session_string_enc=enc,
                                     is_active=True))
    log.info("Telegram сессия сохранена для %s", phone)


def _done_page(phone: str) -> HTMLResponse:
    return _page("Готово", f"<h1>✓</h1><p>Telegram переподключён: "
                 f"<b>{_esc(phone)}</b></p>"
                 "<p class='mute'>Ингестор подхватит сессию сам — он циклится "
                 "и на ближайшем рестарте (секунды) залогинится заново.</p>"
                 "<p><a href='/sources'>← к источникам</a></p>")
