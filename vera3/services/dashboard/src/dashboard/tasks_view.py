"""HTML страницы `/tasks`: вкладки, строки, карточка задачи. Всё экранируется."""
from __future__ import annotations

from datetime import datetime
from urllib.parse import quote, urlsplit

from vera_shared.db.models_room import RoomTaskEventRow
from vera_shared.room.attention import humanize
from vera_shared.timeutil import utc_naive_now

from dashboard.render import esc
from dashboard.tasks_gantt import CSS as GANTT_CSS
from dashboard.tasks_gantt import DEFAULT_SPAN, GanttRow, task_gantt, tasks_gantt
from dashboard.tasks_questions_view import Questions, questions_block
from dashboard.tasks_service import TABS, TaskItem, plan_of, segments_of

TAB_LABELS = {"work": "В работе", "me": "Нужен я", "done": "Готово"}
EMPTY = {"work": "Задач в работе пока нет", "me": "Ничего не ждёт вашего внимания",
         "done": "Завершённых задач пока нет"}
PRIORITY = {0: "срочно", 1: "высокий", 2: "обычный", 3: "низкий"}
PROGRESS_CHARS = 140
_WARN = ("needs_owner", "lease_expired", "stale_progress")

_CSS = """<style>
.tk-row{display:block;padding:.7rem .9rem;border:1px solid var(--line);border-radius:var(--r-md);margin:.5rem 0;cursor:pointer}
.tk-row:hover{border-color:var(--accent-line)}.tk-row b{color:var(--text-strong)}
.tk-meta{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;margin-top:.3rem}
.tk-detail dl{display:grid;grid-template-columns:max-content 1fr;gap:.25rem .9rem}
.tk-detail dd{margin:0;overflow-wrap:anywhere}.tk-ev{margin:.2rem 0;overflow-wrap:anywhere}
.tk-q{border:1px solid var(--line);border-radius:var(--r-md);padding:.5rem .7rem;margin:.5rem 0}
.tk-answer textarea{width:100%;box-sizing:border-box;margin:.3rem 0}
</style>"""


def ago(at: datetime | None, now: datetime) -> str:
    return f"{humanize(now - at)} назад" if at else "—"


def _trim(text: str | None) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= PROGRESS_CHARS else t[:PROGRESS_CHARS - 1] + "…"


def _href(item: TaskItem) -> str:
    return f"/tasks/{quote(item.row.room, safe='')}/{quote(item.row.task_id, safe='')}"


def tabs_nav(active: str, counts: dict[str, int], span: str = DEFAULT_SPAN) -> str:
    extra = "" if span == DEFAULT_SPAN else f"&amp;span={esc(span)}"
    chips = "".join(
        f'<a class="chip{" on" if t == active else ""}" href="/tasks?tab={t}{extra}">'
        f'{TAB_LABELS[t]} {counts[t]}</a>' for t in TABS)
    return f'<div class="chips">{chips}</div>'


def task_row(item: TaskItem, now: datetime) -> str:
    r, a = item.row, item.attention
    who = esc(r.lease_holder or r.owner or "никто")
    acct = f" · {esc(r.holder_account)}" if r.holder_account and r.lease_holder else ""
    tone = "warn" if a.state in _WARN else ("ok" if a.state == "done" else "off")
    progress = ""
    if r.last_progress_text:
        progress = (f'<div class="muted">{esc(_trim(r.last_progress_text))} '
                    f'· {esc(ago(r.last_progress_at, now))}</div>')
    return (f'<a class="tk-row" href="{_href(item)}" hx-get="{_href(item)}" '
            f'hx-target="#task-detail" hx-swap="innerHTML">'
            f'<b>{esc(r.title or r.task_id)}</b>'
            f'<div class="tk-meta"><span class="pill {tone}">{esc(a.label_ru)}</span>'
            f'<span class="muted">{who}{acct}</span>'
            f'<span class="muted">{esc(PRIORITY.get(r.priority, r.priority))}</span></div>'
            f'{progress}</a>')


def tasks_body(tab: str, tabs: dict[str, list[TaskItem]], now: datetime | None = None,
               gantt_rows: list[GanttRow] | None = None, span: str = DEFAULT_SPAN) -> str:
    now = now or utc_naive_now()
    rows = "".join(task_row(i, now) for i in tabs[tab]) or f'<p class="muted">{EMPTY[tab]}</p>'
    counts = {t: len(v) for t, v in tabs.items()}
    gantt = tasks_gantt(gantt_rows, now, span, tab) if gantt_rows is not None else ""
    return (f'{_CSS}{GANTT_CSS}<h1>Задачи</h1>{tabs_nav(tab, counts, span)}{gantt}'
            f'{rows}<div id="task-detail"></div>')


def _link_ref(kind: str, ref: str) -> str:
    if kind == "url" and urlsplit(ref).scheme in ("http", "https"):
        return f'<a href="{esc(ref)}" rel="noopener noreferrer" target="_blank">{esc(ref)}</a>'
    return f"{esc(kind)}: {esc(ref)}"


def _field(label: str, value: str) -> str:
    return f'<dt class="muted">{label}</dt><dd>{value}</dd>'


def _event(e: RoomTaskEventRow, now: datetime) -> str:
    text = f" — {esc(e.text)}" if e.text else ""
    return (f'<div class="tk-ev"><span class="muted">{esc(f"{e.at:%Y-%m-%d %H:%M}")} UTC · '
            f'{esc(ago(e.at, now))}</span> <b>{esc(e.kind)}</b> '
            f'<span class="muted">{esc(e.agent or "—")}</span>{text}</div>')


def task_detail(item: TaskItem, events: list[RoomTaskEventRow],
                now: datetime | None = None, questions: Questions | None = None) -> str:
    now = now or utc_naive_now()
    r, a = item.row, item.attention
    refs = "<br>".join(_link_ref(str(x.get("kind", "")), str(x.get("ref", "")))
                       for x in (r.refs or []) if isinstance(x, dict)) or "—"
    fields = "".join((
        _field("Задача", esc(r.task_id)), _field("Комната", esc(r.room)),
        _field("Статус", f"{esc(r.status)} · {esc(a.label_ru)}"),
        _field("Приоритет", esc(PRIORITY.get(r.priority, r.priority))),
        _field("Держатель", esc(r.lease_holder or "—")),
        _field("Аккаунт", esc(r.holder_account or "—")),
        _field("Владелец", esc(r.owner or "—")),
        _field("Проект", esc(r.project or "—")),
        _field("Следующий шаг", esc(r.next_action or "—")),
        _field("Последний результат", esc(r.last_progress_text or "—")),
        _field("Ссылки", refs),
    ))
    log = "".join(_event(e, now) for e in events) or '<p class="muted">Событий нет</p>'
    return (f'<article class="tk-detail"><h2>{esc(r.title or r.task_id)}</h2>'
            f'<dl>{fields}</dl>{questions_block(questions or [])}'
            f'{task_gantt(segments_of(r, events, now), plan_of(r))}'
            f'<h3>История</h3>{log}</article>')
