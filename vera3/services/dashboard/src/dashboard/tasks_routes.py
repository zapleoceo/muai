"""`/tasks` — задачи комнаты, `/tasks/{room}/{task_id}` — карточка для htmx, `/answer` — ответ владельца."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from vera_shared.room.questions import QuestionNotFound, QuestionState
from vera_shared.timeutil import utc_naive_now

from dashboard.csrf import owner_post_gate
from dashboard.render import _render, esc, owner_or_blank_401, owner_or_redirect
from dashboard.tasks_gantt import resolve_span
from dashboard.tasks_service import (
    TABS,
    load_detail,
    load_gantt_rows,
    load_tabs,
    submit_answer,
)
from dashboard.tasks_view import task_detail, tasks_body
from dashboard.watchdog_view import load_watchdog, watchdog_badge

router = APIRouter()


def _problem(msg: str, code: int) -> HTMLResponse:
    return HTMLResponse(f'<p class="muted">{esc(msg)}</p>', status_code=code)


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request, tab: str = "work", span: str | None = None):
    if (resp := owner_or_redirect(request)) is not None:
        return resp
    tab = tab if tab in TABS else "work"
    tabs = await load_tabs()
    now = utc_naive_now()
    rows = await load_gantt_rows(tabs[tab], now)
    span = resolve_span(span, rows, now)
    badge = watchdog_badge(await load_watchdog(), now)
    return HTMLResponse(_render("tasks", tasks_body(tab, tabs, now, rows, span, badge)))


async def _fragment(room: str, task_id: str) -> HTMLResponse:
    found = await load_detail(room, task_id)
    if found is None:
        return _problem("Задача не найдена", 404)
    item, events, qs = found
    return HTMLResponse(task_detail(item, events, questions=qs))


@router.get("/tasks/{room}/{task_id}", response_class=HTMLResponse)
async def task_fragment(request: Request, room: str, task_id: str):
    if (resp := owner_or_blank_401(request)) is not None:
        return resp
    return await _fragment(room, task_id)


@router.post("/tasks/{room}/{task_id}/answer", response_class=HTMLResponse)
async def task_answer(request: Request, room: str, task_id: str,
                      qid: int = Form(..., ge=1, le=2**31 - 1), text: str = Form(...)):  # noqa: B008
    if (denied := owner_post_gate(request)) is not None:
        return denied
    try:
        await submit_answer(room, task_id, qid, text)
    except QuestionNotFound as e:
        return _problem(str(e), 404)
    except QuestionState as e:
        return _problem(str(e), 409)
    except ValueError as e:
        return _problem(str(e), 422)
    return await _fragment(room, task_id)
