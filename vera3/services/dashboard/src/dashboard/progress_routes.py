"""Live-progress fragment (HTMX poll every 30s) + backfill pause/rate controls."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from vera_shared.control import (
    get_backfill_max_per_hour,
    is_backfill_paused,
    set_backfill_max_per_hour,
    set_backfill_paused,
)
from vera_shared.db.engine import get_session
from vera_shared.db.models_sources import GmailAccountRow
from vera_shared.timeutil import utc_naive_now

from dashboard.progress_view import PROGRESS_STYLE, pause_controls, rate_controls
from dashboard.render import esc, local_dt, owner_or_blank_401
from dashboard.stats import get_stats

router = APIRouter()


@router.get("/_progress", response_class=HTMLResponse)
async def progress_fragment(request: Request):
    if (resp := owner_or_blank_401(request)) is not None:
        return resp
    return HTMLResponse(await _build_progress_fragment())


@router.post("/control/backfill", response_class=HTMLResponse)
async def control_backfill(request: Request, action: str = Form(...)):
    """Pause/resume the brain-triage + media backfill. Owner-only. Returns the
    refreshed progress fragment so HTMX swaps it in place."""
    if (resp := owner_or_blank_401(request)) is not None:
        return resp
    await set_backfill_paused(action == "pause")
    return HTMLResponse(await _build_progress_fragment())


@router.post("/control/backfill-rate", response_class=HTMLResponse)
async def control_backfill_rate(request: Request, max_per_hour: int = Form(0)):
    """Set the even-tempo backfill request cap (per hour). 0 = unlimited.
    Owner-only. Returns the refreshed progress fragment."""
    if (resp := owner_or_blank_401(request)) is not None:
        return resp
    await set_backfill_max_per_hour(max(0, max_per_hour))
    return HTMLResponse(await _build_progress_fragment())


def _eta(remaining: int, per_hour: float) -> str:
    """«Сколько ещё» — по темпу ИМЕННО этой очереди."""
    if per_hour <= 0 or remaining <= 0:
        return "—"
    h = remaining / per_hour
    if h < 2:
        return f"~{int(h * 60)} мин"
    return f"~{h:.1f} ч" if h < 48 else f"~{h / 24:.1f} дн"


def _pct(done: int, total: int) -> int:
    return min(100, int(100 * done / total)) if total > 0 else 0


async def _build_progress_fragment() -> str:
    now = utc_naive_now()
    paused = await is_backfill_paused()
    max_per_hour = await get_backfill_max_per_hour()

    # Всё тяжёлое — из общего кэша (один проход по БД раз в TTL, см. stats.py).
    st = await get_stats()
    ingest_1h = st["ingest_1h"]
    ingest_24h = st["ingest_24h"]
    triage_1h = st["triage_1h"]
    triage_24h = st["triage_24h"]
    pending = st["pending"]
    media_pending = st["media_pending"]
    errored = st["error"]
    dead = st["dead"]
    per_source_1h = st["per_source_1h"]

    async with get_session() as s:
        # Gmail-аккаунты — лёгкий запрос (3 строки), держим живым (не кэшируем).
        gmail = (await s.execute(
            select(GmailAccountRow).order_by(GmailAccountRow.id)
        )).scalars().all()

    # Две очереди, две скорости, два ETA. Раньше ETA был один и считался как
    # `весь backlog / темп ТРИАЖА` — а backlog почти целиком состоит из фото,
    # которые идут через vision в десять раз медленнее. 02.09 это дало «442
    # события, ETA 3.3 ч» при настоящих 36 часах.
    triage_queue = pending + errored          # dead не считаем: retry не поможет
    eta_triage = _eta(triage_queue, triage_1h)
    vision_per_h = st["vision_24h"] / 24
    media_left = st["media_backlog_left"]
    media_total = st["media_backlog_total"]
    eta_media = _eta(media_left, vision_per_h)

    src_chips = "".join(
        f'<span class="chip">{esc(src)}: <b>+{cnt:,}</b></span>'
        for src, cnt in per_source_1h
    ) or '<span class="mute">за последний час событий не поступало</span>'

    gmail_rows = []
    for g in gmail:
        last = local_dt(g.last_polled_at, "time")
        ago = ""
        if g.last_polled_at:
            mins = int((now - g.last_polled_at).total_seconds() / 60)
            ago = f" ({mins}м назад)"
        gmail_rows.append(
            f'<div class="row"><span>📧 {esc(g.email)}</span>'
            f'<span class="mute">последний опрос: {last}{ago}</span></div>'
        )

    # Полоса меряет долю СДЕЛАННОГО от всего объёма. Раньше знаменателем была
    # работа за последние сутки (`backlog + triage_24h`), поэтому полоса росла,
    # когда Вера больше работала, и падала, когда крон доливал очередь, —
    # чем угодно, только не прогрессом.
    pct_triage = _pct(st["done"], st["done"] + triage_queue)
    pct_media = _pct(media_total - media_left, media_total)

    pause_ui = pause_controls(paused)
    rate_ui = rate_controls(max_per_hour)

    return f"""
      <p class="muted small">Обновляется каждые 30 секунд.</p>

      <div class="bf-control">{pause_ui}</div>
      <div class="bf-control">{rate_ui}</div>

      <div class="prog-grid">
        <div class="prog-cell">
          <div class="prog-label">Приходят события</div>
          <div class="prog-big">+{ingest_1h:,}<span class="prog-unit"> за час</span></div>
          <div class="mute small">{ingest_24h:,} за последние 24ч</div>
        </div>
        <div class="prog-cell">
          <div class="prog-label">Триажируется AI</div>
          <div class="prog-big">{triage_1h:,}<span class="prog-unit">/час</span></div>
          <div class="mute small">{triage_24h:,} за последние 24ч</div>
        </div>
        <div class="prog-cell">
          <div class="prog-label">В очереди на триаж</div>
          <div class="prog-big">{triage_queue:,}</div>
          <div class="mute small">ETA: {eta_triage}</div>
          <div class="mute small">
            ⏳ {pending:,} ждут
            {' · ❗ ' + f'{errored:,} на повторе' if errored else ''}
            {' · 💀 ' + f'{dead:,} отказ' if dead else ''}
          </div>
        </div>
        <div class="prog-cell">
          <div class="prog-label">Распознавание медиа</div>
          <div class="prog-big">{media_left:,}<span class="prog-unit"> осталось</span></div>
          <div class="mute small">ETA: {eta_media}</div>
          <div class="mute small">
            🎬 {media_pending:,} в работе · {vision_per_h:.0f}/час
          </div>
        </div>
      </div>

      <div style="margin:14px 0">
        <div class="mute small">
          Триаж: разобрано {st['done']:,} из {st['done'] + triage_queue:,}
        </div>
        <div class="bar"><div class="bar-fill" style="width:{pct_triage}%"></div></div>
      </div>

      <div style="margin:14px 0">
        <div class="mute small">
          Распознавание: {media_total - media_left:,} из {media_total:,}
          (в очереди держится рабочее окно, а не весь остаток)
        </div>
        <div class="bar"><div class="bar-fill" style="width:{pct_media}%"></div></div>
      </div>

      <div style="margin:18px 0 8px">
        <b style="font-size:13px">За последний час поступило:</b><br>
        <div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:6px">{src_chips}</div>
      </div>

      <div style="margin-top:18px">
        <b style="font-size:13px">Почтовые ящики Gmail:</b>
        {''.join(gmail_rows) if gmail_rows else '<div class="mute">нет аккаунтов</div>'}
      </div>

      {PROGRESS_STYLE}
    """
