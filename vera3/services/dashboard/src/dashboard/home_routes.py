"""Home page (`/`) — top-line stats cards, live-progress shell, search box."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from dashboard.render import _render, esc, format_eta, owner_or_redirect, row_list
from dashboard.stats import get_stats

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if (resp := owner_or_redirect(request)) is not None:
        return resp

    st = await get_stats()
    total_events = st["total"]
    triaged = st["done"]
    pending = st["backlog_total"]        # вся очередь, не только pending
    with_emb = st["with_emb"]
    cost_today = st["cost_today"]
    calls_today = st["calls_today"]
    cost_month = st["cost_month"]
    triage_1h = st["triage_1h"]
    earliest = st["earliest"]

    pct_triaged = 100 * triaged // max(total_events, 1)
    pct_emb = 100 * with_emb // max(total_events, 1)

    sources_html = row_list(
        (esc(src), f"{cnt:,}") for src, cnt in st["sources_top"][:5]
    )
    eta_txt = format_eta(pending, triage_1h)

    earliest_txt = earliest.strftime("%d %b %Y") if earliest else "—"
    history_days = (datetime.utcnow() - earliest).days if earliest else 0

    return HTMLResponse(_render(
        "home",
        f"""
        <div class="cards">
          <div class="card"><div class="card-label">События</div>
            <div class="card-value">{total_events:,}</div>
            <div class="card-sub">всего в мозге · глубина {history_days} дн (с {earliest_txt})</div></div>
          <div class="card" title="Триаж = AI прочитал событие и расставил теги важности/тем/людей. Идёт в фоне через brain-triage, free LLM пул.">
            <div class="card-label">Триаж <span style="font-size:10px;color:#666">(ⓘ)</span></div>
            <div class="card-value">{triaged:,}<small> ({pct_triaged}%)</small></div>
            <div class="card-sub">{pending:,} в очереди · ETA {eta_txt}</div></div>
          <div class="card" title="Embeddings = семантический вектор Voyage для поиска по смыслу. Делается одновременно с триажем.">
            <div class="card-label">Embeddings <span style="font-size:10px;color:#666">(ⓘ)</span></div>
            <div class="card-value">{with_emb:,}<small> ({pct_emb}%)</small></div>
            <div class="card-sub">для семантического поиска</div></div>
          <div class="card"><div class="card-label">$ сегодня</div>
            <div class="card-value">${cost_today:.4f}</div>
            <div class="card-sub">{calls_today:,} LLM-вызовов · мес ${cost_month:.2f}</div></div>
        </div>

        <div id="live-progress" class="section" hx-get="/_progress" hx-trigger="load, every 30s" hx-swap="innerHTML">
          <h2>📥 Live прогресс</h2>
          <div class="mute" style="font-size:13px">загружается…</div>
        </div>

        <div class="section">
          <h2>Спросить Веру</h2>
          <form hx-post="/search-ui" hx-target="#answer" hx-swap="innerHTML"
                hx-indicator="#spin">
            <input type="text" name="q" placeholder="кто такой Дмитрий Егоров?"
                   autocomplete="off" required>
            <div style="margin-top:10px;display:flex;align-items:center;gap:12px">
              <button type="submit">Спросить →</button>
              <span id="spin" class="htmx-indicator mute">⏳ ищу…</span>
            </div>
          </form>
          <div id="answer"></div>
        </div>

        <div class="section">
          <h2>Источники событий</h2>
          {sources_html}
        </div>
        """
    ))
