"""Settings page (`/settings`) — runtime-editable thresholds (SETTINGS
registry) plus a read-only view of deploy-time env params."""
from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from vera_shared.control import SETTINGS, get_settings_values, set_control

from dashboard.render import _render, esc, owner_or_redirect
from dashboard.ui.components import collapsible

router = APIRouter()

# Deploy-time параметры (env/compose) — только для справки, меняются передеплоем.
_DEPLOY_PARAMS = [
    ("BRAIN_TRIAGE_REPLICAS", "5", "Сколько реплик триаж-воркеров. Больше = "
     "быстрее разбор очереди (упирается в брокер). Меняется в docker-compose."),
    ("TRIAGE_CONCURRENCY", "10", "Параллельных LLM-вызовов на одну реплику."),
    ("TRIAGE_BATCH_SIZE", "16", "Сколько событий воркер берёт за один заход."),
    ("TRIAGE_POLL_INTERVAL_S", "5", "Пауза между заходами когда очередь пуста, сек."),
    ("GMAIL_POLL_S", "300", "Как часто опрашиваются Gmail-ящики, сек."),
    ("VERA_DAILY_GLOBAL_CAP_USD", "2.0", "Дневной потолок трат (страховка). "
     "Ключи и биллинг живут в брокере."),
]


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if (resp := owner_or_redirect(request)) is not None:
        return resp

    values = await get_settings_values()
    rows = []
    for s in SETTINGS:
        val = values.get(s.key, s.default)
        if s.kind == "bool":
            checked_on = "selected" if val == "1" else ""
            checked_off = "selected" if val != "1" else ""
            field = (f'<select name="{s.key}">'
                     f'<option value="1" {checked_on}>вкл</option>'
                     f'<option value="0" {checked_off}>выкл</option></select>')
        else:
            field = (f'<input type="number" name="{s.key}" value="{esc(val)}" '
                     f'>')
        rows.append(
            f'<div class="set-row"><div class="set-main">'
            f'<label>{esc(s.label)}</label>'
            f'<div class="set-desc">{esc(s.desc)}</div></div>'
            f'<div class="set-field">{field}'
            f'<span class="set-unit">{esc(s.unit)}</span></div></div>'
        )

    deploy_rows = "".join(
        f'<div class="row"><span>{esc(name)} '
        f'<span class="muted small">{esc(desc)}</span></span>'
        f'<span class="mute"><code>{esc(os.environ.get(name, dflt))}</code></span></div>'
        for name, dflt, desc in _DEPLOY_PARAMS
    )

    advanced = f"""
      <h4>Монитор и разбор (меняются на лету)</h4>
      <form method="post" action="/control/settings">
        {''.join(rows)}
        <button type="submit">Сохранить</button>
      </form>
      <h4>Параметры развёртывания (только чтение)</h4>
      <p class="muted small">Задаются в <code>infra/.env</code> и docker-compose,
        меняются переразвёртыванием.</p>
      {deploy_rows}
    """
    body = f"""
    <h2>Настройки</h2>
    <p class="muted">Обычно сюда заходить не нужно: Вера работает с настройками по умолчанию.</p>
    {collapsible("Дополнительно", advanced)}
    <h4>Прочее</h4>
    <p><a href="/entities/duplicates">Дубли людей</a>
       <span class="muted small"> — объединение повторяющихся карточек</span></p>

    <style>
      .set-row {{ display:flex; justify-content:space-between; align-items:flex-start;
                  gap:1.2rem; padding:.9rem 0; border-bottom:1px solid var(--vera-line); }}
      .set-main label {{ font-weight:600; }}
      .set-desc {{ color:var(--vera-muted); font-size:.85rem; margin-top:.2rem; max-width:32rem; }}
      .set-field {{ white-space:nowrap; display:flex; align-items:center; gap:.4rem; }}
      .set-field input, .set-field select {{ width:8rem; margin:0; }}
      .set-unit {{ color:var(--vera-muted); font-size:.85rem; }}
    </style>
    """
    return HTMLResponse(_render("settings", body))


@router.post("/control/settings")
async def control_settings(request: Request):
    if (resp := owner_or_redirect(request)) is not None:
        return resp
    form = await request.form()
    for s in SETTINGS:
        raw = form.get(s.key)
        if raw is None:
            continue
        if s.kind == "bool":
            val = "1" if str(raw) == "1" else "0"
        else:
            try:
                val = str(max(0, int(raw)))
            except (ValueError, TypeError):
                continue
        await set_control(s.key, val)
    return RedirectResponse("/settings", status_code=303)
