"""Owner-only live hourly report within the Vera dashboard."""
from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from dashboard.csrf import owner_gate, owner_post_gate
from dashboard.office_card_details import safe_text
from dashboard.office_hourly_service import load_recent_task_events, load_report
from dashboard.office_hourly_view import modal_body, report_body
from dashboard.office_owner_cancel import apply_pending_cancellations
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


@router.post("/office/hourly/apply-cancellations", response_model=None)
async def office_apply_cancellations(request: Request):
    if (denied := owner_post_gate(request)) is not None:
        return denied
    await apply_pending_cancellations()
    return RedirectResponse("/office/hourly", status_code=303)

@router.get("/office/hourly/mobile", response_model=None)
async def office_mobile(request: Request) -> Response:
    """Minimal native-widget projection; same owner gate and Office scope."""
    if (denied := owner_gate(request)) is not None:
        denied.headers["Cache-Control"] = "private, no-store"
        return denied
    report = await load_report(include_closed=False)
    tasks = []
    for card in report.cards:
        question = card.question
        tasks.append({
            "room": card.row.room,
            "id": card.row.task_id,
            "title": safe_text(card.row.title or card.row.task_id, 300),
            "status": card.status,
            "next_action": safe_text(card.row.next_action, 2000),
            "question": {
                "id": question.qid,
                "text": safe_text(question.question, 16000),
            } if question is not None and question.status == "open" else None,
        })
    tasks.sort(key=lambda task: task["question"] is None)
    return JSONResponse(
        {"schema": 1, "as_of": report.as_of.isoformat(), "tasks": tasks},
        headers={"Cache-Control": "private, no-store", "Vary": "Cookie"},
    )
