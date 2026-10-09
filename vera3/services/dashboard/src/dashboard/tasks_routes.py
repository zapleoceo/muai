"""`/tasks` — задачи комнаты (только чтение), `/tasks/{room}/{task_id}` — карточка для htmx."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from dashboard.render import _render, owner_or_blank_401, owner_or_redirect
from dashboard.tasks_service import TABS, load_detail, load_tabs
from dashboard.tasks_view import task_detail, tasks_body

router = APIRouter()


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request, tab: str = "work"):
    if (resp := owner_or_redirect(request)) is not None:
        return resp
    tab = tab if tab in TABS else "work"
    return HTMLResponse(_render("tasks", tasks_body(tab, await load_tabs())))


@router.get("/tasks/{room}/{task_id}", response_class=HTMLResponse)
async def task_fragment(request: Request, room: str, task_id: str):
    if (resp := owner_or_blank_401(request)) is not None:
        return resp
    found = await load_detail(room, task_id)
    if found is None:
        return HTMLResponse('<p class="muted">Задача не найдена</p>', status_code=404)
    return HTMLResponse(task_detail(*found))
