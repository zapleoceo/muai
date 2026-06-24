"""Vera 3.0 dashboard — Telegram-auth, все секции работают."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from html import escape as _esc
from typing import AsyncIterator

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select, text


def esc(v) -> str:
    """HTML-escape для значений из БД/Telegram. Защита от XSS.

    Telethon тащит user-controlled chat_title/sender_username/usernames в БД —
    они идут в рендеринг как-есть. Любой пользователь может назвать чат
    `<script>...</script>` и получить XSS в дашборде.
    """
    if v is None:
        return "—"
    return _esc(str(v), quote=True)

from vera_shared.db.engine import close_engine, get_session, init_engine
from vera_shared.db.models import EventRow, TokenRow, UsageLogRow
from vera_shared.db.models_sources import GmailAccountRow, InstagramSessionRow, TelegramSessionRow

from dashboard.auth import (
    COOKIE_NAME, TTL, get_bot_username, issue_session,
    require_owner, verify_telegram_auth, OWNER_ID,
)

log = logging.getLogger(__name__)
SEARCH_URL = os.environ.get("SEARCH_URL", "http://brain-search:8000")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_engine()
    yield
    await close_engine()


app = FastAPI(title="Vera 3.0 Dashboard", lifespan=lifespan)


def _set_session_cookie(resp: Response) -> None:
    cookie, ttl = issue_session()
    resp.set_cookie(
        COOKIE_NAME, cookie,
        max_age=ttl, httponly=True, secure=True, samesite="lax", path="/",
    )


# ─── Auth ──────────────────────────────────────────────────────────────────


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    bot = get_bot_username()
    return HTMLResponse(_LOGIN_HTML.replace("__BOT__", bot))


@app.get("/api/tg_login")
async def tg_login(request: Request):
    data = dict(request.query_params)
    user_id = verify_telegram_auth(data)
    if user_id is None:
        return HTMLResponse(_AUTH_ERROR.replace("__MSG__", "Невалидная подпись Telegram"), status_code=403)
    if user_id != OWNER_ID:
        return HTMLResponse(_AUTH_ERROR.replace("__MSG__", f"Доступ запрещён для user_id {user_id}"), status_code=403)
    resp = RedirectResponse(url="/", status_code=303)
    _set_session_cookie(resp)
    return resp


@app.get("/api/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "dashboard"}


# ─── Главная ───────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    try:
        require_owner(request, request.cookies.get(COOKIE_NAME))
    except HTTPException:
        return RedirectResponse(url="/login", status_code=303)

    async with get_session() as s:
        total_events = (await s.execute(select(func.count(EventRow.id)))).scalar() or 0
        triaged = (await s.execute(select(func.count(EventRow.id)).where(EventRow.triage_status == "done"))).scalar() or 0
        pending = (await s.execute(select(func.count(EventRow.id)).where(EventRow.triage_status == "pending"))).scalar() or 0
        errors = (await s.execute(select(func.count(EventRow.id)).where(EventRow.triage_status == "error"))).scalar() or 0
        with_emb = (await s.execute(
            select(func.count(EventRow.id)).where(EventRow.embedding_voyage_3.is_not(None))
        )).scalar() or 0

        today = datetime.utcnow().date()
        cost_today = (await s.execute(
            select(func.coalesce(func.sum(UsageLogRow.cost_usd), 0.0))
            .where(UsageLogRow.created_at >= today)
        )).scalar() or 0.0
        calls_today = (await s.execute(
            select(func.count(UsageLogRow.id)).where(UsageLogRow.created_at >= today)
        )).scalar() or 0
        cost_month = (await s.execute(
            select(func.coalesce(func.sum(UsageLogRow.cost_usd), 0.0))
            .where(UsageLogRow.created_at >= today - timedelta(days=30))
        )).scalar() or 0.0

        # Top sources
        sources = (await s.execute(text(
            "SELECT source, COUNT(*) FROM events GROUP BY source ORDER BY 2 DESC LIMIT 5"
        ))).all()

        # Backfill progress: events added in last hour vs last 24h
        hour_ago = datetime.utcnow() - timedelta(hours=1)
        day_ago = datetime.utcnow() - timedelta(hours=24)
        rate_1h = (await s.execute(
            select(func.count(EventRow.id)).where(EventRow.received_at >= hour_ago)
        )).scalar() or 0
        rate_24h = (await s.execute(
            select(func.count(EventRow.id)).where(EventRow.received_at >= day_ago)
        )).scalar() or 0
        # Triage rate
        triage_1h = (await s.execute(text(
            "SELECT COUNT(*) FROM usage_log WHERE workflow='triage' AND created_at >= :t"
        ), {"t": hour_ago})).scalar() or 0
        # Earliest event date (depth of history)
        earliest = (await s.execute(
            select(func.min(EventRow.occurred_at))
        )).scalar()

    pct_triaged = 100 * triaged // max(total_events, 1)
    pct_emb = 100 * with_emb // max(total_events, 1)

    sources_html = "".join(
        f'<div class="row"><span>{esc(src)}</span><span class="mute">{cnt:,}</span></div>'
        for src, cnt in sources
    )

    # ETA для триажа
    if triage_1h > 0 and pending > 0:
        eta_hours = pending / triage_1h
        if eta_hours < 2:
            eta_txt = f"~{int(eta_hours * 60)} мин"
        elif eta_hours < 48:
            eta_txt = f"~{eta_hours:.1f} ч"
        else:
            eta_txt = f"~{eta_hours / 24:.1f} дн"
    else:
        eta_txt = "—"

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

        <div class="section" hx-get="/_progress" hx-trigger="load, every 10s" hx-swap="innerHTML">
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


# ─── Live progress fragment (HTMX poll, обновляется каждые 10с) ──────────────


@app.get("/_progress", response_class=HTMLResponse)
async def progress_fragment(request: Request):
    try:
        require_owner(request, request.cookies.get(COOKIE_NAME))
    except HTTPException:
        return HTMLResponse("", status_code=401)

    from datetime import datetime as dt, timedelta as td
    now = dt.utcnow()

    async with get_session() as s:
        # Темп прихода событий
        ingest_1h = (await s.execute(
            select(func.count(EventRow.id)).where(EventRow.received_at >= now - td(hours=1))
        )).scalar() or 0
        ingest_24h = (await s.execute(
            select(func.count(EventRow.id)).where(EventRow.received_at >= now - td(hours=24))
        )).scalar() or 0
        # Темп триажа
        triage_1h = (await s.execute(text(
            "SELECT COUNT(*) FROM usage_log WHERE workflow='triage' AND created_at >= :t"
        ), {"t": now - td(hours=1)})).scalar() or 0
        triage_24h = (await s.execute(text(
            "SELECT COUNT(*) FROM usage_log WHERE workflow='triage' AND created_at >= :t"
        ), {"t": now - td(hours=24)})).scalar() or 0
        # Pending count
        pending = (await s.execute(
            select(func.count(EventRow.id)).where(EventRow.triage_status == "pending")
        )).scalar() or 0
        # Per-source за последний час (что льётся)
        per_source_1h = (await s.execute(text(
            "SELECT source, COUNT(*) FROM events WHERE received_at >= :t "
            "GROUP BY source ORDER BY 2 DESC"
        ), {"t": now - td(hours=1)})).all()
        # Активные Gmail аккаунты + их прогресс
        gmail = (await s.execute(
            select(GmailAccountRow).order_by(GmailAccountRow.id)
        )).scalars().all()

    # ETA
    if triage_1h > 0 and pending > 0:
        eta_h = pending / triage_1h
        eta = f"~{int(eta_h * 60)} мин" if eta_h < 2 else (
              f"~{eta_h:.1f} ч" if eta_h < 48 else f"~{eta_h/24:.1f} дн")
    else:
        eta = "—"

    src_chips = "".join(
        f'<span class="chip">{esc(src)}: <b>+{cnt:,}</b></span>'
        for src, cnt in per_source_1h
    ) or '<span class="mute">за последний час событий не поступало</span>'

    gmail_rows = []
    for g in gmail:
        last = g.last_polled_at.strftime("%H:%M") if g.last_polled_at else "—"
        ago = ""
        if g.last_polled_at:
            mins = int((now - g.last_polled_at).total_seconds() / 60)
            ago = f" ({mins}м назад)"
        gmail_rows.append(
            f'<div class="row"><span>📧 {esc(g.email)}</span>'
            f'<span class="mute">last poll: {last}{ago}</span></div>'
        )

    # Progress bar для триажа
    total_events = pending + (triage_24h if triage_24h else 1)
    pct_pending = min(100, int(100 * pending / max(total_events, 1)))

    return HTMLResponse(f"""
      <h2>📥 Live прогресс <span style="font-size:12px;color:#888">(обновляется каждые 10с)</span></h2>

      <div class="prog-grid">
        <div class="prog-cell">
          <div class="prog-label">Приходят события</div>
          <div class="prog-big">+{ingest_1h:,}<span class="prog-unit"> за час</span></div>
          <div class="mute" style="font-size:12px">{ingest_24h:,} за последние 24ч</div>
        </div>
        <div class="prog-cell">
          <div class="prog-label">Триажируется AI</div>
          <div class="prog-big">{triage_1h:,}<span class="prog-unit">/час</span></div>
          <div class="mute" style="font-size:12px">{triage_24h:,} за последние 24ч</div>
        </div>
        <div class="prog-cell">
          <div class="prog-label">В очереди на триаж</div>
          <div class="prog-big">{pending:,}</div>
          <div class="mute" style="font-size:12px">ETA: {eta}</div>
        </div>
      </div>

      <div style="margin:14px 0">
        <div class="mute" style="font-size:12px;margin-bottom:6px">
          Прогресс триажа (обработано / накопилось за сутки):
        </div>
        <div class="bar"><div class="bar-fill" style="width:{100 - pct_pending}%"></div></div>
      </div>

      <div style="margin:18px 0 8px">
        <b style="font-size:13px">За последний час поступило:</b><br>
        <div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:6px">{src_chips}</div>
      </div>

      <div style="margin-top:18px">
        <b style="font-size:13px">Gmail ingestor:</b>
        {''.join(gmail_rows) if gmail_rows else '<div class="mute">нет аккаунтов</div>'}
      </div>

      <style>
        .prog-grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                      gap:14px; margin:14px 0; }}
        .prog-cell {{ background:#0f1115; border:1px solid #2a2d34; border-radius:10px; padding:14px; }}
        .prog-label {{ font-size:11px; color:#888; text-transform:uppercase; letter-spacing:0.05em; }}
        .prog-big {{ font-size:26px; font-weight:600; margin:6px 0 3px; }}
        .prog-unit {{ font-size:13px; color:#888; font-weight:400; margin-left:4px; }}
        .bar {{ background:#0f1115; height:8px; border-radius:4px; overflow:hidden;
                border:1px solid #2a2d34; }}
        .bar-fill {{ background:linear-gradient(90deg,#4dabf7,#6dd687); height:100%;
                     transition:width 1s ease; }}
        .chip {{ display:inline-block; padding:4px 10px; background:#0f1115;
                 border:1px solid #2a2d34; border-radius:999px; font-size:12px; }}
      </style>
    """)


# ─── Tokens ────────────────────────────────────────────────────────────────


@app.get("/tokens", response_class=HTMLResponse)
async def tokens_page(request: Request, _=Depends(lambda r=Request: None)):
    try:
        require_owner(request, request.cookies.get(COOKIE_NAME))
    except HTTPException:
        return RedirectResponse("/login", status_code=303)

    # local pool — теперь только cold fallback. Real source of truth = aibroker.
    async with get_session() as s:
        rows = (await s.execute(
            select(TokenRow).order_by(TokenRow.provider, TokenRow.id)
        )).scalars().all()

    broker_url = os.environ.get("BROKER_URL", "").rstrip("/")
    broker_link = broker_url + "/dashboard" if broker_url else None

    fallback_rows = "".join(
        f'<tr><td>{esc(r.provider)}</td><td>{esc(r.label)}</td>'
        f'<td>{esc(r.tier)}</td>'
        f'<td>{"✓" if r.is_active else "✗"}</td></tr>'
        for r in rows
    )

    body = f"""
    <div style="background:#1a3d5a; border:1px solid #2a5d7a; color:#aed4ee;
                padding:20px 24px; border-radius:12px; margin-bottom:24px;">
      <div style="font-size:14px; color:#cfdfe7; margin-bottom:6px;">
        🔑 Ключи теперь управляются централизованным брокером
      </div>
      <div style="font-size:13px; color:#9ab; line-height:1.6;">
        После миграции 2026-06-24 все LLM-вызовы Vera идут через
        <b>aibroker</b> (CRUD, health-monitor, ротация, кэпы — всё там).
        Локальный пул ниже — холодный fallback, используется только если
        broker недоступен.
      </div>
      {('<div style="margin-top:14px;"><a href="' + esc(broker_link) +
        '" target="_blank" style="display:inline-block; background:#4dabf7; '
        'color:#0f1115; padding:10px 18px; border-radius:8px; '
        'font-weight:600; text-decoration:none; font-size:13px;">'
        '→ Открыть aibroker dashboard</a></div>') if broker_link else ''}
    </div>

    <h2 style="font-size:14px; color:#888; font-weight:500;
               text-transform:uppercase; letter-spacing:0.05em;">
      Локальный fallback-пул ({len(rows)} ключей)
    </h2>
    <table class="data">
      <thead><tr><th>provider</th><th>label</th><th>tier</th><th>active</th></tr></thead>
      <tbody>{fallback_rows}</tbody>
    </table>
    <p style="font-size:11px; color:#666; margin-top:8px;">
      В fallback-пул не пишется live-state (используется counter, cooldown,
      cost) — это кеш на случай аварии broker'а.
    </p>
    """
    return HTMLResponse(_render("tokens", body))


# ─── Events ────────────────────────────────────────────────────────────────


@app.get("/events", response_class=HTMLResponse)
async def events_page(request: Request, limit: int = Query(100, ge=1, le=500),
                       source: str | None = None,
                       status: str | None = None):
    try:
        require_owner(request, request.cookies.get(COOKIE_NAME))
    except HTTPException:
        return RedirectResponse("/login", status_code=303)

    q = select(EventRow).order_by(EventRow.occurred_at.desc()).limit(limit)
    if source:
        q = q.where(EventRow.source == source)
    if status:
        q = q.where(EventRow.triage_status == status)

    async with get_session() as s:
        rows = (await s.execute(q)).scalars().all()

    tbody = []
    for e in rows:
        status_emoji = {"done": "✓", "pending": "⏳", "processing": "⏳",
                        "error": "✗"}.get(e.triage_status, "?")
        imp = e.importance if e.importance is not None else "—"
        preview = esc((e.content_text or "")[:160])
        tbody.append(
            f'<tr><td>{e.id}</td><td>{status_emoji}</td><td>{imp}</td>'
            f'<td>{esc(e.source)}</td><td>{esc(e.account or "—")}</td>'
            f'<td class="mute">{e.occurred_at.strftime("%Y-%m-%d %H:%M")}</td>'
            f'<td class="preview">{preview}…</td></tr>'
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

    return HTMLResponse(_render("events", f"""
        <h2>События ({len(rows)})</h2>
        {filters}
        <table class="data">
          <thead><tr><th>id</th><th>tr</th><th>imp</th><th>src</th>
          <th>account</th><th>time</th><th>preview</th></tr></thead>
          <tbody>{''.join(tbody)}</tbody>
        </table>
    """))


# ─── Gmail accounts ────────────────────────────────────────────────────────


def self_ig_block(ig_sessions, ig_total, ig_1h, ig_24h, ig_last,
                   ig_by_direction, ig_top_threads, now) -> str:
    rows = []
    for s in ig_sessions:
        state = "✓ active" if s.is_active else "✗ inactive"
        state_cls = "ok" if s.is_active else "err"
        last_poll = s.last_polled_at.strftime("%Y-%m-%d %H:%M:%S") if s.last_polled_at else "никогда"
        rows.append(
            f'<tr><td>{s.id}</td><td>@{esc(s.username)}</td>'
            f'<td class="pill {state_cls}">{state}</td>'
            f'<td>{last_poll}</td></tr>'
        )

    last_txt = ig_last.strftime("%Y-%m-%d %H:%M:%S") if ig_last else "никогда"
    mins = int((now - ig_last).total_seconds() / 60) if ig_last else None
    if mins is None:
        freshness = '<span class="pill err">нет данных</span>'
    elif mins < 10:
        freshness = f'<span class="pill ok">живой ({mins} мин назад)</span>'
    elif mins < 120:
        freshness = f'<span class="pill warn">тихо ({mins} мин)</span>'
    else:
        freshness = f'<span class="pill err">давно молчит ({mins} мин)</span>'

    dir_html = "".join(
        f'<div class="row"><span>{esc(d)}</span><span class="mute">{cnt:,}</span></div>'
        for d, cnt in ig_by_direction
    ) or '<div class="mute">—</div>'

    threads_html = "".join(
        f'<div class="row"><span>{esc((title or "")[:60])} '
        f'<span class="mute">({"group" if is_group=="true" else "direct"})</span></span>'
        f'<span class="mute">{cnt:,}</span></div>'
        for title, is_group, cnt in ig_top_threads
    ) or '<div class="mute">пока нет данных</div>'

    return f"""
        <h2 style="margin-top:32px">📸 Instagram</h2>
        <div style="margin-bottom:12px">Статус потока: {freshness}</div>
        <table class="data">
          <thead><tr><th>id</th><th>username</th><th>state</th><th>last polled</th></tr></thead>
          <tbody>{''.join(rows) or '<tr><td colspan=4 class="mute">нет сессий</td></tr>'}</tbody>
        </table>

        <div class="cards" style="margin-top:14px">
          <div class="card"><div class="card-label">Всего DM-событий</div>
            <div class="card-value">{ig_total:,}</div>
            <div class="card-sub">последнее {last_txt}</div></div>
          <div class="card"><div class="card-label">За час</div>
            <div class="card-value">+{ig_1h:,}</div>
            <div class="card-sub">{ig_24h:,} за 24ч</div></div>
        </div>

        <div class="two-col" style="margin-top:14px">
          <div class="section">
            <h3 style="margin-top:0;font-size:14px">По направлению</h3>
            {dir_html}
            <div class="mute" style="font-size:11px;margin-top:8px">
              <b>received</b> = входящие в DM · <b>sent</b> = ваши исходящие
            </div>
          </div>
          <div class="section">
            <h3 style="margin-top:0;font-size:14px">Топ-20 диалогов</h3>
            {threads_html}
          </div>
        </div>
    """


@app.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request):
    try:
        require_owner(request, request.cookies.get(COOKIE_NAME))
    except HTTPException:
        return RedirectResponse("/login", status_code=303)

    now = datetime.utcnow()
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(hours=24)

    async with get_session() as s:
        gmail_rows = (await s.execute(
            select(GmailAccountRow).order_by(GmailAccountRow.id)
        )).scalars().all()
        tg_sessions = (await s.execute(
            select(TelegramSessionRow).order_by(TelegramSessionRow.id)
        )).scalars().all()
        ig_sessions = (await s.execute(
            select(InstagramSessionRow).order_by(InstagramSessionRow.id)
        )).scalars().all()
        events_by_src = (await s.execute(text(
            "SELECT source, COUNT(*) FROM events GROUP BY source ORDER BY 2 DESC"
        ))).all()
        # Telegram стата
        tg_total = (await s.execute(text(
            "SELECT COUNT(*) FROM events WHERE source='telegram'"
        ))).scalar() or 0
        tg_1h = (await s.execute(text(
            "SELECT COUNT(*) FROM events WHERE source='telegram' AND received_at >= :t"
        ), {"t": hour_ago})).scalar() or 0
        tg_24h = (await s.execute(text(
            "SELECT COUNT(*) FROM events WHERE source='telegram' AND received_at >= :t"
        ), {"t": day_ago})).scalar() or 0
        tg_by_type = (await s.execute(text(
            "SELECT COALESCE(metadata->>'chat_type', category) AS t, COUNT(*) "
            "FROM events WHERE source='telegram' GROUP BY 1 ORDER BY 2 DESC"
        ))).all()
        tg_by_direction = (await s.execute(text(
            "SELECT COALESCE(metadata->>'direction','?'), COUNT(*) "
            "FROM events WHERE source='telegram' GROUP BY 1 ORDER BY 2 DESC"
        ))).all()
        tg_top_chats = (await s.execute(text(
            "SELECT COALESCE(metadata->>'chat_title','(unknown)'), "
            "COALESCE(metadata->>'chat_type','?'), COUNT(*) "
            "FROM events WHERE source='telegram' "
            "GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20"
        ))).all()
        tg_last = (await s.execute(text(
            "SELECT MAX(received_at) FROM events WHERE source='telegram'"
        ))).scalar()
        # Instagram стата
        ig_total = (await s.execute(text(
            "SELECT COUNT(*) FROM events WHERE source='instagram'"
        ))).scalar() or 0
        ig_1h = (await s.execute(text(
            "SELECT COUNT(*) FROM events WHERE source='instagram' AND received_at >= :t"
        ), {"t": hour_ago})).scalar() or 0
        ig_24h = (await s.execute(text(
            "SELECT COUNT(*) FROM events WHERE source='instagram' AND received_at >= :t"
        ), {"t": day_ago})).scalar() or 0
        ig_by_direction = (await s.execute(text(
            "SELECT COALESCE(metadata->>'direction','?'), COUNT(*) "
            "FROM events WHERE source='instagram' GROUP BY 1 ORDER BY 2 DESC"
        ))).all()
        ig_top_threads = (await s.execute(text(
            "SELECT COALESCE(metadata->>'thread_title','(unknown)'), "
            "COALESCE((metadata->>'is_group')::text,'false'), COUNT(*) "
            "FROM events WHERE source='instagram' "
            "GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20"
        ))).all()
        ig_last = (await s.execute(text(
            "SELECT MAX(received_at) FROM events WHERE source='instagram'"
        ))).scalar()

    # Gmail
    gmail_html = []
    for g in gmail_rows:
        async with get_session() as s:
            ev_count = (await s.execute(
                select(func.count(EventRow.id))
                .where(EventRow.source == "gmail", EventRow.account == g.email)
            )).scalar() or 0
        last = g.last_polled_at.strftime("%Y-%m-%d %H:%M") if g.last_polled_at else "никогда"
        state = "✓ active" if g.is_active else "✗ inactive"
        state_cls = "ok" if g.is_active else "err"
        gmail_html.append(
            f'<tr><td>{g.id}</td><td>{esc(g.email)}</td>'
            f'<td class="pill {state_cls}">{state}</td>'
            f'<td>{last}</td><td>{ev_count:,}</td></tr>'
        )

    # Telegram session info
    tg_session_rows = []
    for t in tg_sessions:
        state = "✓ active" if t.is_active else "✗ inactive"
        state_cls = "ok" if t.is_active else "err"
        tg_session_rows.append(
            f'<tr><td>{t.id}</td><td>{esc(t.phone)}</td>'
            f'<td class="pill {state_cls}">{state}</td>'
            f'<td>{t.created_at.strftime("%Y-%m-%d") if t.created_at else "—"}</td></tr>'
        )

    tg_last_txt = tg_last.strftime("%Y-%m-%d %H:%M:%S") if tg_last else "никогда"
    mins_since = int((now - tg_last).total_seconds() / 60) if tg_last else None
    if mins_since is None:
        tg_freshness = '<span class="pill err">мёртвый</span>'
    elif mins_since < 5:
        tg_freshness = f'<span class="pill ok">живой (последнее {mins_since} мин назад)</span>'
    elif mins_since < 60:
        tg_freshness = f'<span class="pill warn">тихо ({mins_since} мин)</span>'
    else:
        tg_freshness = f'<span class="pill err">давно молчит ({mins_since} мин)</span>'

    tg_types_html = "".join(
        f'<div class="row"><span>{esc(t or "—")}</span><span class="mute">{cnt:,}</span></div>'
        for t, cnt in tg_by_type
    )
    tg_dir_html = "".join(
        f'<div class="row"><span>{esc(d)}</span><span class="mute">{cnt:,}</span></div>'
        for d, cnt in tg_by_direction
    )
    tg_top_html = "".join(
        f'<div class="row"><span>{esc((title or "")[:60])} '
        f'<span class="mute">({esc(ctype or "?")})</span></span>'
        f'<span class="mute">{cnt:,}</span></div>'
        for title, ctype, cnt in tg_top_chats
    )

    src_html = "".join(
        f'<div class="row"><span>{esc(src)}</span><span class="mute">{cnt:,} событий</span></div>'
        for src, cnt in events_by_src
    )

    return HTMLResponse(_render("sources", f"""
        <h2>📧 Gmail аккаунты</h2>
        <table class="data">
          <thead><tr><th>id</th><th>email</th><th>state</th>
          <th>last polled</th><th>events</th></tr></thead>
          <tbody>{''.join(gmail_html) or '<tr><td colspan=5 class="mute">нет аккаунтов</td></tr>'}</tbody>
        </table>

        <h2 style="margin-top:32px">✈️ Telegram userbot</h2>
        <div style="margin-bottom:12px">Статус потока: {tg_freshness}</div>
        <table class="data">
          <thead><tr><th>id</th><th>phone</th><th>state</th><th>created</th></tr></thead>
          <tbody>{''.join(tg_session_rows) or '<tr><td colspan=4 class="mute">нет сессий</td></tr>'}</tbody>
        </table>

        <div class="cards" style="margin-top:14px">
          <div class="card"><div class="card-label">Всего сообщений</div>
            <div class="card-value">{tg_total:,}</div>
            <div class="card-sub">последнее {tg_last_txt}</div></div>
          <div class="card"><div class="card-label">За час</div>
            <div class="card-value">+{tg_1h:,}</div>
            <div class="card-sub">{tg_24h:,} за 24ч</div></div>
        </div>

        <div class="two-col" style="margin-top:14px">
          <div class="section">
            <h3 style="margin-top:0;font-size:14px">По типу чата</h3>
            {tg_types_html or '<div class="mute">—</div>'}
            <div class="mute" style="font-size:11px;margin-top:8px">
              <b>user</b> = личка · <b>chat</b> = малая группа · <b>channel</b> = канал или супергруппа
            </div>
          </div>
          <div class="section">
            <h3 style="margin-top:0;font-size:14px">По направлению</h3>
            {tg_dir_html or '<div class="mute">—</div>'}
            <div class="mute" style="font-size:11px;margin-top:8px">
              <b>received</b> = входящие · <b>sent</b> = ваши исходящие
            </div>
          </div>
        </div>

        <div class="section" style="margin-top:14px">
          <h3 style="margin-top:0;font-size:14px">Топ-20 чатов по объёму</h3>
          {tg_top_html or '<div class="mute">пока нет данных</div>'}
        </div>

        {self_ig_block(ig_sessions, ig_total, ig_1h, ig_24h, ig_last, ig_by_direction, ig_top_threads, now)}

        <div class="section" style="margin-top:24px">
          <h2>Все источники в БД</h2>
          {src_html}
        </div>

        <style>
          .two-col {{ display:grid; grid-template-columns: 1fr 1fr; gap:14px; }}
          @media (max-width: 800px) {{ .two-col {{ grid-template-columns: 1fr; }} }}
          .pill.warn {{ background:#3d2f0a; color:#ffd84a; }}
        </style>
    """))


# ─── Search proxy ──────────────────────────────────────────────────────────


@app.post("/search-ui", response_class=HTMLResponse)
async def search_ui(request: Request, q: str = Form(...)):
    try:
        require_owner(request, request.cookies.get(COOKIE_NAME))
    except HTTPException:
        return HTMLResponse('<div class="error">Auth required</div>', status_code=401)
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(f"{SEARCH_URL}/search", json={"q": q, "limit": 15})
        data = r.json()
        # Полный HTML escape ответа + перевод \n в <br>. quote=True закрывает
        # XSS через атрибуты, не только теги.
        answer = esc(data.get("answer", "—")).replace("\n", "<br>")
        provider = esc(data.get("provider") or "—")
        cost = float(data.get("cost_usd", 0.0))
        n = len(data.get("results", []))
        return HTMLResponse(
            f'<div class="answer"><b>Ответ:</b><br>{answer}</div>'
            f'<div class="meta">via {provider}, ${cost:.4f}, {n} событий</div>'
        )
    except Exception as e:
        return HTMLResponse(f'<div class="error">Ошибка: {esc(str(e))}</div>')


# ─── Templates ─────────────────────────────────────────────────────────────


def _render(active: str, body: str) -> str:
    nav = []
    items = [("home", "/", "главная"), ("tokens", "/tokens", "токены"),
             ("events", "/events", "события"), ("sources", "/sources", "источники")]
    for key, href, label in items:
        cls = "active" if active == key else ""
        nav.append(f'<a href="{href}" class="{cls}">{label}</a>')
    nav.append('<a href="/api/logout" style="margin-left:auto;color:#888">выйти</a>')

    return _HTML_HEAD.replace("__NAV__", "".join(nav)) + body + _HTML_FOOT


_HTML_HEAD = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Vera 3.0</title>
<script src="https://unpkg.com/htmx.org@1.9.10"></script>
<style>
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #0f1115; color: #e4e6eb; max-width: 1200px;
       margin: 0 auto; padding: 24px; line-height: 1.5; }
h1, h2 { font-weight: 600; margin: 0 0 16px; letter-spacing: -0.01em; }
h2 { font-size: 18px; margin-top: 0; }
a { color: #4dabf7; text-decoration: none; }
nav { display: flex; gap: 6px; margin-bottom: 24px; padding: 6px;
      background: #1a1d24; border-radius: 10px; }
nav a { padding: 8px 14px; border-radius: 6px; color: #aab; }
nav a:hover { background: #2a2d34; color: #e4e6eb; }
nav a.active { background: #2a2d34; color: #fff; font-weight: 600; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
         gap: 14px; margin: 0 0 24px; }
.card { background: #1a1d24; border: 1px solid #2a2d34; border-radius: 12px; padding: 18px; }
.card-label { font-size: 11px; color: #888; text-transform: uppercase;
              letter-spacing: 0.06em; }
.card-value { font-size: 32px; font-weight: 600; margin: 8px 0 4px; }
.card-value small { font-size: 14px; color: #888; font-weight: 400; }
.card-sub { font-size: 12px; color: #888; }
.section { background: #1a1d24; border-radius: 12px; padding: 20px; margin: 16px 0; }
.row { display: flex; justify-content: space-between; padding: 8px 0;
       border-bottom: 1px solid #2a2d34; }
.row:last-child { border-bottom: none; }
.mute { color: #888; }
table.data { width: 100%; border-collapse: collapse; font-size: 13px; }
table.data th, table.data td { padding: 9px 10px; text-align: left;
                                border-bottom: 1px solid #2a2d34; vertical-align: top; }
table.data th { color: #888; font-weight: 500; text-transform: uppercase; font-size: 11px; }
.pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px;
        font-weight: 500; }
.pill.ok { background: #14422c; color: #6dd687; }
.pill.warn { background: #4a3a14; color: #ffc864; }
.pill.err { background: #4a1a1d; color: #ffaaaa; }
.preview { color: #ccc; font-family: 'SF Mono', Monaco, monospace; max-width: 600px;
           overflow: hidden; text-overflow: ellipsis; }
input, select, textarea { padding: 10px 12px; border-radius: 8px; background: #0f1115;
       border: 1px solid #2a2d34; color: #e4e6eb; font-size: 14px; font-family: inherit; }
input[type=text] { width: 100%; padding: 14px; font-size: 15px; }
button { padding: 11px 22px; background: #4dabf7; color: white; border: none;
         border-radius: 8px; font-weight: 600; cursor: pointer; font-size: 14px; }
button:hover { background: #3a9ce0; }
.answer { background: #0f1115; padding: 18px; border-radius: 10px; margin: 14px 0;
          line-height: 1.6; border: 1px solid #2a2d34; }
.meta { color: #888; font-size: 12px; margin-top: 6px; }
.error { background: #4a1a1d; padding: 14px; border-radius: 8px; color: #ffaaaa; }
.htmx-indicator { display: none; }
.htmx-request .htmx-indicator { display: inline; }
.htmx-request.htmx-indicator { display: inline; }
</style></head><body>
<nav>__NAV__</nav>"""

_HTML_FOOT = "</body></html>"


_LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Vera 3.0 — вход</title>
<style>
body { font-family: -apple-system, sans-serif; background: #0f1115; color: #e4e6eb;
       display: flex; align-items: center; justify-content: center; min-height: 100vh;
       margin: 0; }
.box { background: #1a1d24; padding: 48px; border-radius: 16px; max-width: 420px;
       text-align: center; box-shadow: 0 20px 60px rgba(0,0,0,0.5); }
h1 { font-size: 36px; margin: 0 0 8px; }
p { color: #888; margin: 12px 0 28px; }
.tg-widget { display: flex; justify-content: center; margin-top: 12px; }
</style></head><body><div class="box">
<h1>Vera 3.0</h1>
<p>Авторизация через Telegram</p>
<div class="tg-widget">
<script async src="https://telegram.org/js/telegram-widget.js?22"
        data-telegram-login="__BOT__"
        data-size="large"
        data-radius="10"
        data-auth-url="/api/tg_login"
        data-request-access="write"></script>
</div>
</div></body></html>"""

_AUTH_ERROR = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Доступ запрещён</title>
<style>body{font-family:sans-serif;background:#0f1115;color:#e4e6eb;
       display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.box{background:#4a1a1d;padding:40px;border-radius:16px;text-align:center;color:#ffaaaa;max-width:400px}
h1{margin:0 0 16px}a{color:#ffaaaa}</style></head>
<body><div class="box"><h1>⛔ Доступ запрещён</h1><p>__MSG__</p>
<p><a href="/login">← вернуться</a></p></div></body></html>"""
