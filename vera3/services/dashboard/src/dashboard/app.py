"""Vera 3.0 dashboard — простой HTMX UI для мониторинга."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select, text

from vera_shared.db.engine import close_engine, get_session, init_engine
from vera_shared.db.models import EventRow, TokenRow, UsageLogRow

log = logging.getLogger(__name__)

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")  # пустой = no auth (для локала)
SEARCH_URL = os.environ.get("SEARCH_URL", "http://brain-search:8000")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_engine()
    yield
    await close_engine()


app = FastAPI(title="Vera 3.0 Dashboard", lifespan=lifespan)


def _check_auth(request: Request):
    if not ADMIN_TOKEN:
        return  # dev mode
    token = request.cookies.get("admin_token") or request.query_params.get("t")
    if token != ADMIN_TOKEN:
        raise HTTPException(401, "Auth required")


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "dashboard"}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    _check_auth(request)

    async with get_session() as s:
        total_events = (await s.execute(select(func.count(EventRow.id)))).scalar() or 0
        triaged = (await s.execute(
            select(func.count(EventRow.id)).where(EventRow.triage_status == "done")
        )).scalar() or 0
        pending = (await s.execute(
            select(func.count(EventRow.id)).where(EventRow.triage_status == "pending")
        )).scalar() or 0
        errors = (await s.execute(
            select(func.count(EventRow.id)).where(EventRow.triage_status == "error")
        )).scalar() or 0
        with_emb = (await s.execute(
            select(func.count(EventRow.id))
            .where(EventRow.embedding_voyage_3.is_not(None))
        )).scalar() or 0

        today = datetime.utcnow().date()
        cost_today = (await s.execute(
            select(func.coalesce(func.sum(UsageLogRow.cost_usd), 0.0))
            .where(UsageLogRow.created_at >= today)
        )).scalar() or 0.0
        calls_today = (await s.execute(
            select(func.count(UsageLogRow.id))
            .where(UsageLogRow.created_at >= today)
        )).scalar() or 0

    pct_triaged = 100 * triaged // max(total_events, 1)
    pct_emb = 100 * with_emb // max(total_events, 1)

    return HTMLResponse(_render_home(
        total_events=total_events, triaged=triaged, pending=pending,
        errors=errors, with_emb=with_emb, pct_triaged=pct_triaged,
        pct_emb=pct_emb, cost_today=cost_today, calls_today=calls_today,
    ))


@app.get("/tokens", response_class=HTMLResponse)
async def tokens_page(request: Request):
    _check_auth(request)
    async with get_session() as s:
        rows = (await s.execute(
            select(TokenRow).order_by(TokenRow.provider, TokenRow.id)
        )).scalars().all()

    tbody = []
    for r in rows:
        in_cd = r.cooldown_until and r.cooldown_until > datetime.utcnow()
        state = (
            "✗ dead" if not r.is_active
            else "◐ cooldown" if in_cd
            else "✓ live"
        )
        tier_emoji = {"free": "🟢", "paid": "🔴", "trial": "🟡"}.get(r.tier, "⚪")
        cap = f"${r.daily_cost_cap_usd:.2f}" if r.daily_cost_cap_usd else "—"
        used = f"${r.daily_cost_used_usd:.4f}" if r.daily_cost_used_usd else "$0"
        tbody.append(
            f"<tr><td>{r.id}</td><td>{tier_emoji} {r.tier}</td>"
            f"<td>{r.provider}</td><td>{r.label}</td><td>{state}</td>"
            f"<td>{r.daily_used}/{r.daily_limit}</td>"
            f"<td>{used} / {cap}</td></tr>"
        )

    return HTMLResponse(_PAGE_HEAD + f"""
    <h1><a href="/">←</a> LLM-токены</h1>
    <table class="data">
      <thead><tr><th>id</th><th>tier</th><th>provider</th><th>label</th>
      <th>state</th><th>requests today</th><th>cost today / cap</th></tr></thead>
      <tbody>{''.join(tbody)}</tbody>
    </table>
    {_PAGE_FOOT}""")


@app.get("/events", response_class=HTMLResponse)
async def events_page(request: Request, limit: int = 50):
    _check_auth(request)
    async with get_session() as s:
        rows = (await s.execute(
            select(EventRow).order_by(EventRow.occurred_at.desc()).limit(limit)
        )).scalars().all()

    tbody = []
    for e in rows:
        status_emoji = {"done": "✓", "pending": "⏳", "error": "✗"}.get(e.triage_status, "?")
        imp = e.importance if e.importance is not None else "—"
        preview = (e.content_text or "")[:120].replace("<", "&lt;").replace(">", "&gt;")
        tbody.append(
            f"<tr><td>{e.id}</td><td>{status_emoji}</td>"
            f"<td>{imp}</td><td>{e.source}</td>"
            f"<td>{e.occurred_at.strftime('%Y-%m-%d %H:%M')}</td>"
            f"<td class='preview'>{preview}…</td></tr>"
        )

    return HTMLResponse(_PAGE_HEAD + f"""
    <h1><a href="/">←</a> Последние события</h1>
    <table class="data">
      <thead><tr><th>id</th><th>status</th><th>imp</th><th>source</th>
      <th>time</th><th>preview</th></tr></thead>
      <tbody>{''.join(tbody)}</tbody>
    </table>
    {_PAGE_FOOT}""")


@app.post("/search-ui", response_class=HTMLResponse)
async def search_ui(request: Request, q: str = Form(...)):
    _check_auth(request)
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{SEARCH_URL}/search", json={"q": q, "limit": 15})
        data = r.json()
        answer = data.get("answer", "—").replace("<", "&lt;").replace("\n", "<br>")
        provider = data.get("provider", "—")
        cost = data.get("cost_usd", 0.0)
        n = len(data.get("results", []))
        return HTMLResponse(
            f'<div class="answer"><b>Ответ:</b><br>{answer}</div>'
            f'<div class="meta">via {provider}, ${cost:.4f}, {n} событий</div>'
        )
    except Exception as e:
        return HTMLResponse(f'<div class="error">Ошибка: {e}</div>')


_PAGE_HEAD = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Vera 3.0</title>
<script src="https://unpkg.com/htmx.org@1.9.10"></script>
<style>
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
       background: #0f1115; color: #e4e6eb; max-width: 1100px; margin: 0 auto; padding: 24px; }
h1 { font-weight: 600; margin: 0 0 20px; }
a { color: #4dabf7; text-decoration: none; }
nav a { margin-right: 20px; padding: 6px 10px; border-radius: 6px; background: #1a1d24; }
nav a:hover { background: #2a2d34; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 20px 0; }
.card { background: #1a1d24; border: 1px solid #2a2d34; border-radius: 12px; padding: 16px; }
.card-label { font-size: 12px; color: #888; text-transform: uppercase; letter-spacing: 0.05em; }
.card-value { font-size: 28px; font-weight: 600; margin: 6px 0; }
.card-sub { font-size: 13px; color: #aaa; }
.section { background: #1a1d24; border-radius: 12px; padding: 20px; margin: 20px 0; }
table.data { width: 100%; border-collapse: collapse; font-size: 13px; }
table.data th, table.data td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #2a2d34; }
table.data th { color: #888; font-weight: 500; text-transform: uppercase; font-size: 11px; }
.preview { color: #ccc; font-family: monospace; }
input[type=text], textarea { width: 100%; padding: 12px; border-radius: 8px;
       background: #0f1115; border: 1px solid #2a2d34; color: #e4e6eb; font-size: 15px; }
button { padding: 12px 24px; background: #4dabf7; color: white; border: none; border-radius: 8px;
         font-weight: 600; cursor: pointer; font-size: 14px; }
.answer { background: #1a1d24; padding: 20px; border-radius: 12px; margin: 16px 0; line-height: 1.5; }
.meta { color: #888; font-size: 12px; }
.error { background: #4a1a1d; padding: 16px; border-radius: 8px; color: #ffaaaa; }
</style></head><body>
<nav><a href="/">главная</a><a href="/tokens">токены</a><a href="/events">события</a></nav>"""

_PAGE_FOOT = "</body></html>"


def _render_home(*, total_events, triaged, pending, errors, with_emb,
                 pct_triaged, pct_emb, cost_today, calls_today) -> str:
    return _PAGE_HEAD + f"""
    <h1>Vera 3.0 <span style="font-size:14px;color:#888;font-weight:normal">dashboard</span></h1>

    <div class="cards">
      <div class="card">
        <div class="card-label">События</div>
        <div class="card-value">{total_events:,}</div>
        <div class="card-sub">всего в мозге</div>
      </div>
      <div class="card">
        <div class="card-label">Триаж</div>
        <div class="card-value">{triaged:,} <small>({pct_triaged}%)</small></div>
        <div class="card-sub">{pending:,} в очереди · {errors:,} ошибок</div>
      </div>
      <div class="card">
        <div class="card-label">Embeddings</div>
        <div class="card-value">{with_emb:,} <small>({pct_emb}%)</small></div>
        <div class="card-sub">для семантического поиска</div>
      </div>
      <div class="card">
        <div class="card-label">$ сегодня</div>
        <div class="card-value">${cost_today:.4f}</div>
        <div class="card-sub">{calls_today:,} LLM-вызовов</div>
      </div>
    </div>

    <div class="section">
      <h2 style="margin-top:0">Спросить Веру</h2>
      <form hx-post="/search-ui" hx-target="#answer" hx-swap="innerHTML">
        <input type="text" name="q" placeholder="кто такой Дмитрий Егоров?" autocomplete="off" required>
        <div style="margin-top:10px"><button type="submit">Спросить →</button></div>
      </form>
      <div id="answer"></div>
    </div>

    {_PAGE_FOOT}"""
