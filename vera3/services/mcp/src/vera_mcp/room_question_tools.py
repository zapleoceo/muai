"""MCP-инструменты вопросов владельцу. Ответа здесь нет намеренно: отвечает только дашборд."""
from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import Context
from pydantic import Field
from vera_shared.room import questions

from vera_mcp.auth import client_of
from vera_mcp.room_tools import Ident, Room, _room

Token = Annotated[int, Field(ge=1)]
Qid = Annotated[int, Field(ge=1)]


async def room_task_ask(
    task_id: Ident, fencing_token: Token, ctx: Context,
    question: Annotated[str, Field(min_length=1, max_length=4000)], room: Room = "main",
    refs: Annotated[list[dict[str, Any]] | None, Field(max_length=20)] = None,
) -> dict[str, Any]:
    """Задать вопрос владельцу: задача станет blocked (owner=owner), в комнату уйдёт сообщение со статусом question. Ответит владелец в дашборде; смотри room_task_questions, подтверди room_task_answer_ack. Ask the owner a blocking question."""
    q = await questions.ask(room=_room(room), task_id=task_id, agent=client_of(ctx),
                            fencing_token=fencing_token, question=question, refs=refs)
    return {"ok": True, "question": q}


async def room_task_answer_ack(
    task_id: Ident, fencing_token: Token, qid: Qid, ctx: Context, room: Room = "main",
    withdraw: bool = False,
) -> dict[str, Any]:
    """Подтвердить, что ответ владельца учтён (вопрос acked; когда открытых не осталось — задача снова in_progress). withdraw=true — снять свой вопрос без ответа. Acknowledge the owner's answer, or withdraw your own question."""
    act = questions.withdraw if withdraw else questions.ack
    q = await act(room=_room(room), task_id=task_id, qid=qid, agent=client_of(ctx),
                  fencing_token=fencing_token)
    return {"ok": True, "question": q}


async def room_task_questions(task_id: Ident, room: Room = "main") -> dict[str, Any]:
    """Вопросы задачи с ответами владельца (статусы open/answered/acked/withdrawn), по возрастанию qid. Только чтение. Task questions with answers."""
    return {"room": room, "task_id": task_id,
            "questions": await questions.questions_of(_room(room), task_id)}


QUESTION_TOOLS = (room_task_ask, room_task_answer_ack, room_task_questions)
