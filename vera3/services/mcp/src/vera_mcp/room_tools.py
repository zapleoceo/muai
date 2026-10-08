"""MCP-инструменты комнаты агентов. Автор и держатель — имя токена, не аргумент."""
from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context
from pydantic import Field
from vera_shared.room import messages, tasks

from vera_mcp.auth import client_of

Room = Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,64}$")]
Ident = Annotated[str, Field(min_length=1, max_length=128)]
Agent = Annotated[str, Field(min_length=1, max_length=64)]
Paths = Annotated[list[Annotated[str, Field(max_length=500)]], Field(max_length=50)]
MAX_BODY_CHARS = 20_000
DEFAULT_LEASE_S = 900
MAX_LEASE_S = 4 * 3600


async def room_post(
    body: Annotated[str, Field(min_length=1, max_length=MAX_BODY_CHARS)], ctx: Context,
    room: Room = "main", to: Agent | None = None, task_id: Ident | None = None,
    in_reply_to: Ident | None = None,
    status: Literal["info", "request", "ack", "question", "in_progress", "done",
                    "blocked"] = "info",
    message_id: Ident | None = None,
) -> dict[str, Any]:
    """Написать в комнату (to=None — всем); повтор того же message_id не создаёт дубль. Post a message; resending the same message_id is idempotent."""
    msg, deduped = await messages.post_message(
        room=room, message_id=message_id or f"{client_of(ctx)}-{uuid.uuid4().hex[:12]}",
        from_agent=client_of(ctx), body=body, to_agent=to, task_id=task_id,
        in_reply_to=in_reply_to, status=status)
    return {"ok": True, "deduped": deduped, "message": msg}


async def room_inbox(
    ctx: Context, room: Room = "main",
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ack: bool = True, since_id: Annotated[int | None, Field(ge=0)] = None,
) -> dict[str, Any]:
    """Новые чужие сообщения мне или всем после моего курсора; ack=True сдвигает курсор на прочитанное. Unread messages for me; ack advances my cursor."""
    me = client_of(ctx)
    items, after = await messages.inbox(agent=me, room=room, limit=limit, since_id=since_id)
    cursor = after
    if ack and items:
        cursor = await messages.advance_cursor(me, room, items[-1]["id"])
    return {"agent": me, "room": room, "messages": items, "cursor": cursor,
            "more": len(items) == limit}


async def room_history(
    room: Room = "main", limit: Annotated[int, Field(ge=1, le=200)] = 50,
    before_id: Annotated[int | None, Field(ge=1)] = None, task_id: Ident | None = None,
) -> dict[str, Any]:
    """Вся переписка комнаты (включая личные сообщения), по возрастанию id. Full room transcript, oldest first."""
    return {"room": room, "messages": await messages.history(
        room=room, limit=limit, before_id=before_id, task_id=task_id)}


async def room_task_open(
    task_id: Ident, ctx: Context, room: Room = "main",
    title: Annotated[str | None, Field(max_length=500)] = None, paths: Paths | None = None,
) -> dict[str, Any]:
    """Завести задачу без захвата (чтобы её мог взять другой агент). Create an unclaimed task."""
    task, created = await tasks.open_task(room=room, task_id=task_id, agent=client_of(ctx),
                                          title=title, paths=paths)
    return {"ok": True, "created": created, "task": task}


async def room_task_claim(
    task_id: Ident, ctx: Context, room: Room = "main",
    lease_seconds: Annotated[int, Field(ge=60, le=MAX_LEASE_S)] = DEFAULT_LEASE_S,
    title: Annotated[str | None, Field(max_length=500)] = None, paths: Paths | None = None,
) -> dict[str, Any]:
    """Захватить задачу в аренду; верни fencing_token во все последующие правки. Повтор своим агентом продлевает аренду. Claim a task lease; pass the returned fencing_token to update/release."""
    task = await tasks.claim(room=room, task_id=task_id, agent=client_of(ctx),
                             lease_seconds=lease_seconds, title=title, paths=paths)
    return {"ok": True, "task": task}


async def room_task_update(
    task_id: Ident, fencing_token: Annotated[int, Field(ge=1)], ctx: Context,
    room: Room = "main", status: Literal["in_progress", "blocked"] | None = None,
    note: Annotated[str | None, Field(max_length=4000)] = None,
    extend_seconds: Annotated[int | None, Field(ge=60, le=MAX_LEASE_S)] = None,
) -> dict[str, Any]:
    """Обновить свою задачу (статус/заметка/продление аренды); устаревший fencing_token отвергается. Update a task you hold."""
    task = await tasks.update(room=room, task_id=task_id, agent=client_of(ctx),
                              fencing_token=fencing_token, status=status, note=note,
                              extend_seconds=extend_seconds)
    return {"ok": True, "task": task}


async def room_task_release(
    task_id: Ident, fencing_token: Annotated[int, Field(ge=1)], ctx: Context,
    room: Room = "main", status: Literal["open", "done", "blocked"] = "done",
    note: Annotated[str | None, Field(max_length=4000)] = None,
) -> dict[str, Any]:
    """Отпустить задачу: done — сделано, blocked — упёрлись, open — вернуть в общий пул. Release a task you hold."""
    task = await tasks.release(room=room, task_id=task_id, agent=client_of(ctx),
                               fencing_token=fencing_token, status=status, note=note)
    return {"ok": True, "task": task}


async def room_tasks(
    room: Room = "main",
    status: Literal["open", "in_progress", "blocked", "done"] | None = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Список задач комнаты со статусом и живым держателем аренды. List room tasks."""
    return {"room": room, "tasks": await tasks.list_tasks(room=room, status=status,
                                                          limit=limit)}


ROOM_TOOLS = (room_post, room_inbox, room_history, room_task_open, room_task_claim,
              room_task_update, room_task_release, room_tasks)
