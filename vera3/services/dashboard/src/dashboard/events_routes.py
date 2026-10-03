"""Входящее (`/events`) — события по дням с фильтром, плюс карточка события
`/events/{id}`. Запрос и маршрутизация здесь, разметка списка — в `events_view`."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from vera_shared.db.engine import get_session
from vera_shared.timeutil import utc_naive_now

from dashboard.events_view import (  # noqa: F401
    EVENTS_COLUMN_HINTS,
    PAGE_STEP,
    TRIAGE_STATUS_INFO,
    events_table,
    filter_form,
    more_link,
)
from dashboard.render import (
    _render,
    esc,
    local_dt,
    owner_or_redirect,
    row_list,
)
from dashboard.stats import get_stats

router = APIRouter()


def _like(raw: str) -> str:
    escaped = raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/events", response_class=HTMLResponse)
async def events_page(request: Request,
                       limit: int = Query(PAGE_STEP, ge=1, le=500),  # noqa: B008
                       source: str | None = None,
                       status: str | None = None,
                       q: str = "",
                       tech: str = ""):
    if (resp := owner_or_redirect(request)) is not None:
        return resp

    show_tech = tech == "1"
    # LATERAL-джойн подтягивает ПОСЛЕДНИЙ брокер-вызов по каждому событию
    # (request_id / модель / токены / цена) из usage_log — индекс ix_usage_event.
    where = []
    params: dict[str, Any] = {"limit": limit + 1}
    if source:
        where.append("e.source = :source")
        params["source"] = source
    if status:
        where.append("e.triage_status = :status")
        params["status"] = status
    if q.strip():
        where.append("e.content_text ILIKE :q ESCAPE '\\'")
        params["q"] = _like(q.strip())
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

    has_more = len(rows) > limit
    rows = rows[:limit]
    st = await get_stats()
    link = more_link({"source": source, "status": status, "q": q.strip(),
                      "tech": "1" if show_tech else ""}, limit) if has_more else ""
    return HTMLResponse(_render("events", f"""
        <h2>Входящее</h2>
        {filter_form(st["sources_all"], source, status, q.strip(), show_tech)}
        {events_table(rows, utc_naive_now().date(), show_tech)}
        {link}
    """))


#: Дорожка записи → кто говорил. Это и есть авторство на сегодня: своё
#: авторство доказано устройством, а не догадкой модели.
STREAM_LABEL: dict[str, str] = {"mic": "Я", "system": "собеседник"}


def transcript_html(extra: dict[str, Any] | None) -> str:
    """Стенограмма события → HTML. Пусто, если её нет (старые события)."""
    if not extra or extra.get("kind") != "voice_transcript":
        return ""
    utterances = extra.get("utterances") or []
    if not utterances:
        return ""
    rows = []
    for u in utterances:
        at = float(u.get("at") or 0.0)
        stamp = f"{int(at) // 60:02d}:{int(at) % 60:02d}"
        cls = "self" if u.get("stream") == "mic" else "other"
        # Имя, опознанное по голосу, вместо безликого «собеседник»: в созвоне
        # на пятерых иначе не видно, кто что сказал.
        who = (str(u.get("speaker")).strip() if u.get("speaker")
               else STREAM_LABEL.get(str(u.get("stream")), str(u.get("stream") or "?")))
        # Помеченное эхо — это голос собеседника, пойманный микрофоном. Показать
        # его как речь владельца значит соврать об авторстве, а спрятать —
        # потерять слова: в том же куске бывают и его собственные.
        if u.get("echo"):
            who, cls = f"{who} · эхо", "echo"
        rows.append(
            f'<tr><td class="mute">{stamp}</td>'
            f'<td class="who {cls}">{esc(who)}</td>'
            f'<td>{esc(u.get("text") or "")}</td></tr>')
    echoes = sum(1 for u in utterances if u.get("echo"))
    voices = sorted({str(u["speaker"]).strip() for u in utterances if u.get("speaker")})
    voices_note = (
        f'<p class="mute">Голоса опознаны: {esc(", ".join(voices))}. Реплики с '
        f'одним именем сказал один человек. Имя берётся из заголовка окна в '
        f'разговоре один на один и дальше узнаётся по голосу; «Собеседник N» — '
        f'голос разделён, но назвать его неоткуда.</p>' if voices else "")
    echo_note = (
        f'<p class="mute">Помечено как эхо: {echoes}. Это речь собеседника, '
        f'пойманная микрофоном из динамиков. В выжимку такие реплики не '
        f'попадают, но здесь остаются — в одном куске с эхом бывают и слова '
        f'владельца.</p>' if echoes else "")
    return f"""
      <h3>Стенограмма ({len(utterances)} реплик, {extra.get('chars', 0)} символов)</h3>
      <p class="mute">Дословно, как распознал слушатель. В поиск по мозгу идёт
      только выжимка выше — иначе обрывки перебивали бы её. Здесь текст лежит
      целиком: выжимка сжимает разговор примерно в тридцать раз, а звук не
      хранится вообще.</p>
      {voices_note}
      {echo_note}
      <table class="data transcript"><tbody>{''.join(rows)}</tbody></table>
    """


@router.get("/events/{event_id}", response_class=HTMLResponse)
async def event_page(request: Request, event_id: int):
    """Карточка события: выжимка плюс дословная стенограмма, если она есть."""
    if (resp := owner_or_redirect(request)) is not None:
        return resp

    async with get_session() as s:
        row = (await s.execute(text("""
            SELECT id, source, account, category, occurred_at, importance,
                   nature, project, triage_status, triage_error,
                   content_text, content_extra, metadata
            FROM events WHERE id = :id
        """), {"id": event_id})).mappings().first()

    if row is None:
        return HTMLResponse(_render("events", "<h2>Событие не найдено</h2>"), 404)

    meta = row["metadata"] or {}
    facts = row_list([
        ("источник", esc(row["source"])),
        ("аккаунт", esc(row["account"] or "—")),
        ("вид", esc(row["category"] or "—")),
        ("когда", local_dt(row["occurred_at"], "datetime")),
        ("важность", str(row["importance"]) if row["importance"] is not None else "—"),
        ("природа", esc(row["nature"] or "—")),
        ("проект", esc(row["project"] or "—")),
        ("разбор ИИ", esc(row["triage_status"] or "—")),
        ("где", esc(" / ".join(str(meta.get(k)) for k in ("app", "window_title")
                               if meta.get(k)) or "—")),
    ])
    error = (f'<p class="err">Ошибка разбора: {esc(row["triage_error"])}</p>'
             if row["triage_error"] else "")
    return HTMLResponse(_render("events", f"""
        <h2>Событие {row['id']}</h2>
        {facts}
        {error}
        <h3>Выжимка</h3>
        <pre style="white-space:pre-wrap">{esc(row['content_text'] or '')}</pre>
        {transcript_html(row['content_extra'])}
        <p><a href="/events">← во входящее</a></p>
    """))
