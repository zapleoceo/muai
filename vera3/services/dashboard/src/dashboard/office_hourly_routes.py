"""Owner-only live hourly report within the Vera dashboard."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from dashboard.office_hourly_service import load_recent_task_events, load_report
from dashboard.office_hourly_view import modal_body, report_body
from dashboard.render import _render, owner_or_blank_401, owner_or_redirect
from dashboard.tasks_service import load_detail

router = APIRouter()


@router.get("/office/hourly", response_class=HTMLResponse)
async def office_hourly(request: Request, show_closed: bool = False) -> HTMLResponse:
    if (resp := owner_or_redirect(request)) is not None:
        return resp
    return HTMLResponse(_render("office", report_body(
        await load_report(include_closed=show_closed), show_closed=show_closed), wide=True),
                        headers={"Cache-Control": "private, no-store"})


@router.get("/office/hourly/detail/{room}/{task_id}", response_class=HTMLResponse)
async def office_hourly_detail(request: Request, room: str, task_id: str) -> HTMLResponse:
    if (resp := owner_or_blank_401(request)) is not None:
        return resp
    found = await load_detail(room, task_id)
    if found is None:
        return HTMLResponse("Задача не найдена", status_code=404)
    item, _, questions = found
    events = await load_recent_task_events(room, task_id)
    return HTMLResponse(modal_body(item, events, questions),
                        headers={"Cache-Control": "private, no-store"})
