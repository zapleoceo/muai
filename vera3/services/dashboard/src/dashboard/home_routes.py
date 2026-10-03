"""Главная (`/`) — поиск вперёд: статусная строка, поле «Спросить Веру»."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from vera_shared.timeutil import utc_naive_now

from dashboard.health import assess
from dashboard.render import _render, esc, owner_or_redirect
from dashboard.source_state import disabled_optional
from dashboard.stats import get_sources_overview, get_stats
from dashboard.ui.components import status_dot

router = APIRouter()


def status_line(total: int, last_24h: int, level: str, text: str) -> str:
    return (
        f'<a class="status-line" href="/sources">{status_dot(level)}{esc(text)}'
        f' · {total:,} событий · +{last_24h:,} за сутки</a>'
    )


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if (resp := owner_or_redirect(request)) is not None:
        return resp

    st = await get_stats()
    health = assess(st, await get_sources_overview(), utc_naive_now(),
                    await disabled_optional())

    return HTMLResponse(_render(
        "home",
        f"""
        {status_line(st["total"], st["ingest_24h"], health.level, health.text)}
        <section class="hero">
          <h1>Что вы хотите вспомнить?</h1>
          <p>Вера ищет по письмам, чатам и заметкам и отвечает с опорой на источники.</p>
        </section>
        <form class="ask" hx-post="/search-ui" hx-target="#answer"
              hx-swap="innerHTML" hx-indicator="#spin">
          <input type="text" name="q" placeholder="Спросите Веру: кто такой Дмитрий Егоров?"
                 autocomplete="off" required autofocus data-hotkey-search>
          <button type="submit">Спросить Веру</button>
        </form>
        <p class="ask-hint">Нажмите <kbd>/</kbd>, чтобы встать в поле поиска.</p>
        <div id="spin" class="htmx-indicator skel-stack" aria-hidden="true">
          <div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>
        </div>
        <div id="answer"></div>
        """
    ))
