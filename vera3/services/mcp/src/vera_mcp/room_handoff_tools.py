"""MCP-инструменты передачи задачи: предложение, приём, отказ, отмена."""
from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import Context
from pydantic import Field
from vera_shared.room import handoff

from vera_mcp.auth import client_of
from vera_mcp.room_tools import MAX_LEASE_S, Agent, Ident, Room, _room

Token = Annotated[int, Field(ge=1)]


async def room_task_handoff(
    task_id: Ident, fencing_token: Token, to: Agent, ctx: Context, room: Room = "main",
    note: Annotated[str | None, Field(max_length=4000)] = None,
) -> dict[str, Any]:
    """Предложить задачу агенту to. Аренда остаётся у тебя, пока он не примет (room_task_handoff_accept); ему уйдёт сообщение в комнате. Отозвать — room_task_handoff_decline с cancel=true. Offer a task to another agent; the lease stays yours until accepted."""
    task = await handoff.offer(room=_room(room), task_id=task_id, agent=client_of(ctx),
                               fencing_token=fencing_token, to_agent=to, note=note)
    return {"ok": True, "task": task}


async def room_task_handoff_accept(
    task_id: Ident, ctx: Context, room: Room = "main",
    lease_seconds: Annotated[int, Field(ge=60, le=MAX_LEASE_S)] = handoff.DEFAULT_HANDOFF_LEASE_S,
    session: Annotated[str | None, Field(max_length=128)] = None,
    account: Annotated[str | None, Field(max_length=64)] = None,
) -> dict[str, Any]:
    """Принять переданную тебе задачу: аренда переходит к тебе, fencing_token растёт на 1 (токен отправителя устаревает). Верни новый токен во все правки. Accept a handoff offered to you."""
    task = await handoff.accept(room=_room(room), task_id=task_id, agent=client_of(ctx),
                                session=session, account=account, lease_seconds=lease_seconds)
    return {"ok": True, "task": task}


async def room_task_handoff_decline(
    task_id: Ident, ctx: Context, room: Room = "main",
    fencing_token: Annotated[int | None, Field(ge=1)] = None, cancel: bool = False,
    reason: Annotated[str | None, Field(max_length=4000)] = None,
) -> dict[str, Any]:
    """Получатель отказывается от передачи; cancel=true — отправитель отзывает своё предложение (нужен его fencing_token). Задача остаётся у отправителя. Decline an offer, or cancel your own."""
    me = client_of(ctx)
    if cancel:
        if fencing_token is None:
            raise ValueError("cancel requires fencing_token")
        task = await handoff.cancel(room=_room(room), task_id=task_id, agent=me,
                                    fencing_token=fencing_token, reason=reason)
    else:
        task = await handoff.decline(room=_room(room), task_id=task_id, agent=me,
                                     reason=reason)
    return {"ok": True, "task": task}


HANDOFF_TOOLS = (room_task_handoff, room_task_handoff_accept, room_task_handoff_decline)
