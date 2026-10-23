"""Защищённые действия владельца (B1): wait/resolve, закрытие вопроса, recover, start.

Все POST — за `owner_post_gate` (сессия владельца + same-origin), как `/pause`. Коды:
404 нет задачи/вопроса, 409 состояние (живая аренда, устаревший fencing, финальная задача),
422 неверные поля (в т.ч. `unresolved_question`).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from vera_shared.room.questions import QuestionNotFound, QuestionState
from vera_shared.room.tasks import StaleLease, TaskBusy, TaskNotFound

from dashboard.csrf import owner_post_gate
from dashboard.tasks_routes import _fragment, _problem
from dashboard.tasks_service import (
    owner_close_question,
    owner_recover,
    owner_resolve_wait,
    owner_start,
)

router = APIRouter()


async def _guarded(request: Request, room: str, task_id: str,
                   call: Callable[[], Awaitable[Any]]) -> HTMLResponse:
    """Действие вызывается только после ворот владельца."""
    if (denied := owner_post_gate(request)) is not None:
        return denied
    try:
        await call()
    except (TaskNotFound, QuestionNotFound) as e:
        return _problem(str(e), 404)
    except (TaskBusy, StaleLease, QuestionState) as e:
        return _problem(str(e), 409)
    except ValueError as e:
        return _problem(str(e), 422)
    return await _fragment(room, task_id)


@router.post("/tasks/{room}/{task_id}/wait/resolve", response_class=HTMLResponse)
async def task_wait_resolve(request: Request, room: str, task_id: str,
                            reason: str = Form(...)):  # noqa: B008
    return await _guarded(request, room, task_id,
                          lambda: owner_resolve_wait(room, task_id, reason))


@router.post("/tasks/{room}/{task_id}/questions/{qid}/close", response_class=HTMLResponse)
async def task_question_close(request: Request, room: str, task_id: str, qid: int,
                              reason: str = Form(...),  # noqa: B008
                              expected_fencing_token: int = Form(..., ge=0, le=2**62)):  # noqa: B008
    return await _guarded(request, room, task_id, lambda: owner_close_question(
        room, task_id, qid, reason, expected_fencing_token))


@router.post("/tasks/{room}/{task_id}/recover", response_class=HTMLResponse)
async def task_recover(request: Request, room: str, task_id: str,
                       expected_fencing_token: int = Form(..., ge=0, le=2**62),  # noqa: B008
                       stop_source: str = Form(...),  # noqa: B008
                       effects: str = Form(...),  # noqa: B008
                       residual_risk_ack: bool = Form(False),  # noqa: B008
                       run_ref: str | None = Form(None),  # noqa: B008
                       stop_ref: str | None = Form(None)):  # noqa: B008
    return await _guarded(request, room, task_id, lambda: owner_recover(
        room, task_id, expected_fencing_token, stop_source=stop_source, effects=effects,
        residual_risk_ack=residual_risk_ack, run_ref=run_ref, stop_ref=stop_ref))


@router.post("/tasks/{room}/{task_id}/start", response_class=HTMLResponse)
async def task_start(request: Request, room: str, task_id: str,
                     expected_fencing_token: int = Form(..., ge=0, le=2**62),  # noqa: B008
                     status: str = Form("keep"),  # noqa: B008
                     clear_wait: bool = Form(False),  # noqa: B008
                     auto_pickup: str = Form("keep"),  # noqa: B008
                     reason: str | None = Form(None)):  # noqa: B008
    return await _guarded(request, room, task_id, lambda: owner_start(
        room, task_id, expected_fencing_token, status=status, clear_wait=clear_wait,
        auto_pickup=auto_pickup, reason=reason))
