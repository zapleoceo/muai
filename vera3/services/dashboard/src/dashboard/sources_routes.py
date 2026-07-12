"""Sources page (`/sources`) — Gmail/Telegram/Instagram ingest health,
per-source event counts, top chats/threads."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from vera_shared.db.engine import get_session
from vera_shared.db.models_sources import (
    GmailAccountRow,
    InstagramSessionRow,
    TelegramSessionRow,
)

from dashboard.render import (
    _render,
    data_table,
    esc,
    freshness_pill,
    local_dt,
    owner_or_redirect,
    row_list,
)
from dashboard.stats import get_sources_stats

router = APIRouter()


def _instagram_block(ig_sessions, ig_total, ig_1h, ig_24h, ig_last,
                      ig_by_direction, ig_top_threads, now) -> str:
    rows = "".join(
        f'<tr><td>{s.id}</td><td>@{esc(s.username)}</td>'
        f'<td class="pill {"ok" if s.is_active else "err"}">'
        f'{"✓ active" if s.is_active else "✗ inactive"}</td>'
        f'<td>{local_dt(s.last_polled_at, "datetime_sec", "никогда")}</td></tr>'
        for s in ig_sessions
    )

    last_txt = local_dt(ig_last, "datetime_sec", "никогда")
    freshness = freshness_pill(ig_last, now, live_within_min=10, warn_within_min=120)

    dir_html = row_list((esc(d), f"{cnt:,}") for d, cnt in ig_by_direction)
    threads_html = row_list(
        (f'{esc((title or "")[:60])} '
         f'<span class="mute">({"group" if is_group=="true" else "direct"})</span>',
         f"{cnt:,}")
        for title, is_group, cnt in ig_top_threads
    ) if ig_top_threads else '<div class="mute">пока нет данных</div>'

    any_inactive = any(not s.is_active for s in ig_sessions) or not ig_sessions
    connect_btn = (
        '<a href="/api/instagram/start" '
        'style="display:inline-block;margin:10px 0;padding:10px 18px;'
        'background:#4dabf7;color:#fff;border-radius:8px;font-weight:600">'
        '🔑 Подключить Instagram</a>'
        + ('<div class="mute" style="font-size:12px;margin-top:4px">'
           'Сессия неактивна/отсутствует — жми и войди заново (логин+пароль, '
           'при 2FA/challenge попросит код).</div>' if any_inactive else "")
    )

    return f"""
        <h2 style="margin-top:32px">📸 Instagram</h2>
        <div style="margin-bottom:12px">Статус потока: {freshness}</div>
        {connect_btn}
        {data_table(["id", "username", "state", "last polled"], rows, "нет сессий")}

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


@router.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request):
    if (resp := owner_or_redirect(request)) is not None:
        return resp

    now = datetime.utcnow()

    # Списки аккаунтов/сессий — маленькие таблицы, держим живыми.
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

    # Все тяжёлые агрегаты по events — из кэша (один набор сканов раз в TTL).
    ss = await get_sources_stats()
    events_by_src = ss["events_by_src"]
    tg_total, tg_1h, tg_24h, tg_last = ss["tg_total"], ss["tg_1h"], ss["tg_24h"], ss["tg_last"]
    tg_by_type, tg_by_direction, tg_top_chats = ss["tg_by_type"], ss["tg_by_direction"], ss["tg_top_chats"]
    ig_total, ig_1h, ig_24h, ig_last = ss["ig_total"], ss["ig_1h"], ss["ig_24h"], ss["ig_last"]
    ig_by_direction, ig_top_threads = ss["ig_by_direction"], ss["ig_top_threads"]
    gmail_counts = ss["gmail_counts"]

    # Gmail (per-account count из кэша, без N+1)
    gmail_html_rows = []
    for g in gmail_rows:
        ev_count = gmail_counts.get(g.email, 0)
        last = local_dt(g.last_polled_at, "datetime", "никогда")
        # Честный статус: needs_reauth важнее is_active
        if getattr(g, "needs_reauth", False):
            state, state_cls = "✗ токен отозван", "err"
        elif not g.is_active:
            state, state_cls = "✗ выключен", "err"
        else:
            state, state_cls = "✓ live", "ok"
        err_note = (f'<div class="mute" style="font-size:11px">{esc((g.last_error or "")[:80])}</div>'
                    if getattr(g, "needs_reauth", False) and g.last_error else "")
        gmail_html_rows.append(
            f'<tr><td>{g.id}</td><td>{esc(g.email)}{err_note}</td>'
            f'<td class="pill {state_cls}">{state}</td>'
            f'<td>{last}</td><td>{ev_count:,}</td></tr>'
        )

    any_reauth = any(getattr(g, "needs_reauth", False) for g in gmail_rows)
    reconnect_btn = (
        '<a href="/api/gmail/start" '
        'style="display:inline-block;margin:10px 0;padding:10px 18px;'
        'background:#4dabf7;color:#fff;border-radius:8px;font-weight:600">'
        '🔑 Переподключить Gmail</a>'
        + ('<div class="mute" style="font-size:12px;margin-top:4px">'
           'Один или несколько ящиков отвалились (Google отзывает токены '
           'каждые 7 дней в Testing-режиме). Жми — пройди вход Google заново.'
           '</div>' if any_reauth else "")
    )

    # Telegram session info
    tg_session_rows = "".join(
        f'<tr><td>{t.id}</td><td>{esc(t.phone)}</td>'
        f'<td class="pill {"ok" if t.is_active else "err"}">'
        f'{"✓ active" if t.is_active else "✗ inactive"}</td>'
        f'<td>{local_dt(t.created_at, "date")}</td></tr>'
        for t in tg_sessions
    )

    tg_last_txt = local_dt(tg_last, "datetime_sec", "никогда")
    tg_freshness = freshness_pill(tg_last, now, live_within_min=5, warn_within_min=60)

    tg_types_html = row_list((esc(t or "—"), f"{cnt:,}") for t, cnt in tg_by_type)
    tg_dir_html = row_list((esc(d), f"{cnt:,}") for d, cnt in tg_by_direction)
    tg_top_html = row_list(
        (f'{esc((title or "")[:60])} <span class="mute">({esc(ctype or "?")})</span>', f"{cnt:,}")
        for title, ctype, cnt in tg_top_chats
    ) if tg_top_chats else '<div class="mute">пока нет данных</div>'

    src_html = row_list(
        (esc(src), f"{cnt:,} событий") for src, cnt in events_by_src
    )

    return HTMLResponse(_render("sources", f"""
        <h2>📧 Gmail аккаунты</h2>
        {data_table(["id", "email", "state", "last polled", "events"], "".join(gmail_html_rows), "нет аккаунтов")}
        {reconnect_btn}

        <h2 style="margin-top:32px">✈️ Telegram userbot</h2>
        <div style="margin-bottom:12px">Статус потока: {tg_freshness}</div>
        {data_table(["id", "phone", "state", "created"], tg_session_rows, "нет сессий")}

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
            {tg_types_html}
            <div class="mute" style="font-size:11px;margin-top:8px">
              <b>user</b> = личка · <b>chat</b> = малая группа · <b>channel</b> = канал или супергруппа
            </div>
          </div>
          <div class="section">
            <h3 style="margin-top:0;font-size:14px">По направлению</h3>
            {tg_dir_html}
            <div class="mute" style="font-size:11px;margin-top:8px">
              <b>received</b> = входящие · <b>sent</b> = ваши исходящие
            </div>
          </div>
        </div>

        <div class="section" style="margin-top:14px">
          <h3 style="margin-top:0;font-size:14px">Топ-20 чатов по объёму</h3>
          {tg_top_html}
        </div>

        {_instagram_block(ig_sessions, ig_total, ig_1h, ig_24h, ig_last, ig_by_direction, ig_top_threads, now)}

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
