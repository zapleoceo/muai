"""Вопросы владельцу в карточке задачи: история и форма ответа. Всё экранируется."""
from __future__ import annotations

from urllib.parse import quote

from vera_shared.db.models_room import RoomTaskAnswerRow, RoomTaskQuestionRow

from dashboard.render import esc

Questions = list[tuple[RoomTaskQuestionRow, list[RoomTaskAnswerRow]]]
STATUS_RU = {"open": "ждёт ответа", "answered": "ответ получен, ждёт исполнителя",
             "acked": "ответ принят", "withdrawn": "снят"}
ANSWERABLE = ("open", "answered")


def answer_url(room: str, task_id: str) -> str:
    return f"/tasks/{quote(room, safe='')}/{quote(task_id, safe='')}/answer"


def _form(q: RoomTaskQuestionRow) -> str:
    label = "Дополнить ответ" if q.status == "answered" else "Ответить"
    return (f'<form class="tk-answer" hx-post="{answer_url(q.room, q.task_id)}" '
            f'hx-target="#task-detail" hx-swap="innerHTML">'
            f'<input type="hidden" name="qid" value="{int(q.qid)}">'
            f'<textarea name="text" rows="3" required maxlength="8000" '
            f'aria-label="Ответ на вопрос {int(q.qid)}"></textarea>'
            f'<button type="submit">{label}</button></form>')


def _question(q: RoomTaskQuestionRow, answers: list[RoomTaskAnswerRow]) -> str:
    replies = "".join(
        f'<div class="tk-ev"><span class="muted">{esc(f"{a.answered_at:%Y-%m-%d %H:%M}")} UTC '
        f'· {esc(a.answered_by)}</span> {esc(a.text)}</div>' for a in answers)
    form = _form(q) if q.status in ANSWERABLE else ""
    return (f'<div class="tk-q"><div><b>#{int(q.qid)}</b> '
            f'<span class="pill off">{esc(STATUS_RU.get(q.status, q.status))}</span> '
            f'<span class="muted">{esc(q.asked_by)} · '
            f'{esc(f"{q.asked_at:%Y-%m-%d %H:%M}")} UTC</span></div>'
            f'<div class="tk-ev">{esc(q.question)}</div>{replies}{form}</div>')


def questions_block(questions: Questions) -> str:
    if not questions:
        return ""
    body = "".join(_question(q, a) for q, a in questions)
    return f'<h3>Вопросы владельцу</h3>{body}'
