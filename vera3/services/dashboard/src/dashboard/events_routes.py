"""Events log page (`/events`) — filterable table with per-event triage
status, importance, and (via LATERAL join) the last broker call that
triaged it (model/tokens/cost)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from vera_shared.db.engine import get_session

from dashboard.render import _render, esc, owner_or_redirect

router = APIRouter()

# events.triage_status → (эмодзи в таблице, русское пояснение для title=).
TRIAGE_STATUS_INFO: dict[str, tuple[str, str]] = {
    "done": ("✓", "обработано триажем (важность/проект/темы проставлены)"),
    "pending": ("⏳", "ждёт очереди на обработку триажем"),
    "processing": ("⏳", "обрабатывается прямо сейчас"),
    "error": ("✗", "ошибка при обработке, будет повторная попытка (см. triage_error)"),
    "dead": ("☠", "превышено число попыток — требует ручного разбора"),
    "superseded": ("≈", "заменено похожим более новым событием (семантический дедуп)"),
    "media_pending": ("🖼", "медиа (фото/голос) ждёт vision/распознавания через брокер"),
}

# Заголовки колонок /events — подсказки на русском (title=, наведение мышью).
EVENTS_COLUMN_HINTS: dict[str, str] = {
    "id": "Внутренний ID события в базе",
    "tr": "Статус триажа — обработки события ИИ. Наведите на значок в строке для деталей",
    "imp": "Важность события, 0–100 — оценивает ИИ при триаже. «—» = ещё не оценено",
    "src": "Источник события: telegram / gmail / instagram / manual / monitor и т.д.",
    "account": "Аккаунт, бот или ящик, через который пришло событие",
    "time": "Когда событие произошло (occurred_at)",
    "preview": "Первые символы текста события",
    "req": "ID запроса к брокеру (request_id) — последний LLM-вызов по этому событию",
    "model": "Какая модель отвечала на этот запрос (через aibroker)",
    "tokens": "Токены запроса: вход → выход",
    "cost": "Стоимость запроса к брокеру, USD",
}


@router.get("/events", response_class=HTMLResponse)
async def events_page(request: Request, limit: int = Query(100, ge=1, le=500),  # noqa: B008
                       source: str | None = None,
                       status: str | None = None):
    if (resp := owner_or_redirect(request)) is not None:
        return resp

    # LATERAL-джойн подтягивает ПОСЛЕДНИЙ брокер-вызов по каждому событию
    # (request_id / модель / токены / цена) из usage_log — индекс ix_usage_event.
    where = []
    params: dict[str, Any] = {"limit": limit}
    if source:
        where.append("e.source = :source")
        params["source"] = source
    if status:
        where.append("e.triage_status = :status")
        params["status"] = status
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    async with get_session() as s:
        rows = (await s.execute(text(f"""
            SELECT e.id, e.triage_status, e.importance, e.source, e.account,
                   e.occurred_at, e.content_text, e.nature,
                   EXISTS(SELECT 1 FROM event_embeddings ee WHERE ee.event_id = e.id) AS has_emb,
                   u.request_id, u.model, u.tokens_in, u.tokens_out, u.cost_usd
            FROM events e
            LEFT JOIN LATERAL (
                SELECT request_id, model, tokens_in, tokens_out, cost_usd
                FROM usage_log ul
                WHERE ul.event_id = e.id
                ORDER BY ul.created_at DESC
                LIMIT 1
            ) u ON true
            {where_sql}
            ORDER BY e.occurred_at DESC
            LIMIT :limit
        """), params)).mappings().all()

    tbody = []
    for e in rows:
        emoji, desc = TRIAGE_STATUS_INFO.get(
            e["triage_status"], ("?", "неизвестный статус триажа"))
        status_title = esc(f"{e['triage_status'] or '(пусто)'} — {desc}")
        imp = e["importance"] if e["importance"] is not None else "—"
        preview = esc((e["content_text"] or "")[:160])
        # Три состояния события: свой брокер-вызов / обработано в пачке / ещё не триажено.
        has_own = e["model"] is not None
        in_batch = (not has_own) and e["nature"] is not None and e["has_emb"]
        req = e["request_id"]
        req_cell = f'<span title="{esc(req)}">{esc(req[:8])}…</span>' if req else "—"
        if has_own:
            model = esc(e["model"])
            tokens = f'{e["tokens_in"]}→{e["tokens_out"]}'
            cost = f'${e["cost_usd"]:.5f}'
        elif in_batch:
            model = '<span class="mute" title="классифицировано групповым вызовом — токены учтены в строке первого события пачки">в пачке ✓</span>'
            tokens = '<span class="mute">учтено в пачке</span>'
            cost = "—"
        else:
            model = tokens = cost = "—"
        tbody.append(
            f'<tr><td>{e["id"]}</td><td title="{status_title}">{emoji}</td><td>{imp}</td>'
            f'<td>{esc(e["source"])}</td><td>{esc(e["account"] or "—")}</td>'
            f'<td class="mute">{e["occurred_at"].strftime("%Y-%m-%d %H:%M")}</td>'
            f'<td class="preview">{preview}…</td>'
            f'<td class="mute">{req_cell}</td><td>{model}</td>'
            f'<td class="mute">{tokens}</td><td class="mute">{cost}</td></tr>'
        )

    filters = f"""
      <form method="get" style="display:flex;gap:8px;margin-bottom:14px">
        <select name="source">
          <option value="">— все источники —</option>
          <option value="gmail" {'selected' if source=='gmail' else ''}>gmail</option>
          <option value="telegram" {'selected' if source=='telegram' else ''}>telegram</option>
          <option value="instagram" {'selected' if source=='instagram' else ''}>instagram</option>
          <option value="monitor" {'selected' if source=='monitor' else ''}>monitor</option>
        </select>
        <select name="status">
          <option value="">— любой статус —</option>
          <option value="done" {'selected' if status=='done' else ''}>done</option>
          <option value="pending" {'selected' if status=='pending' else ''}>pending</option>
          <option value="error" {'selected' if status=='error' else ''}>error</option>
        </select>
        <input type="number" name="limit" value="{limit}" min="1" max="500" style="width:80px">
        <button type="submit">фильтр</button>
      </form>
    """

    thead = "".join(
        f'<th title="{esc(hint)}">{col}</th>'
        for col, hint in EVENTS_COLUMN_HINTS.items()
    )
    return HTMLResponse(_render("events", f"""
        <h2>Log ({len(rows)})</h2>
        {filters}
        <table class="data">
          <thead><tr>{thead}</tr></thead>
          <tbody>{''.join(tbody)}</tbody>
        </table>
    """))
