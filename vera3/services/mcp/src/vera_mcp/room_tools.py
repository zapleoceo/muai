"""MCP-инструменты комнаты агентов. Автор и держатель — имя токена, не аргумент."""
from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context
from pydantic import Field
from vera_shared.room import messages, tasks

from vera_mcp.auth import allowed_rooms, client_of

Room = Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,64}$")]
Ident = Annotated[str, Field(min_length=1, max_length=128)]
Agent = Annotated[str, Field(min_length=1, max_length=64)]
Consumer = Annotated[str, Field(pattern=r"^[a-z0-9_.-]{1,64}$")]
Paths = Annotated[list[Annotated[str, Field(max_length=500)]], Field(max_length=50)]
MAX_BODY_CHARS = 20_000
DEFAULT_LEASE_S = 900
MAX_LEASE_S = 4 * 3600


def _room(room: str) -> str:
    if room not in allowed_rooms():
        raise ValueError(f"room {room!r} is not open to room tokens")
    return room


async def room_post(
    body: Annotated[str, Field(min_length=1, max_length=MAX_BODY_CHARS)], ctx: Context,
    room: Room = "main", to: Agent | None = None, task_id: Ident | None = None,
    in_reply_to: Ident | None = None,
    status: Literal["info", "request", "ack", "question", "in_progress", "done",
                    "blocked"] = "info",
    message_id: Ident | None = None, session: Ident | None = None,
) -> dict[str, Any]:
    """Написать в комнату (to=None — всем; to — адресация, не приватность). Повтор message_id с тем же содержимым — не дубль, с другим — ошибка. session — метка твоего runtime для аудита. Post a message; the same message_id is idempotent."""
    me = client_of(ctx)
    msg, deduped = await messages.post_message(
        room=_room(room), message_id=message_id or f"{me}-{uuid.uuid4().hex[:12]}",
        from_agent=me, body=body, to_agent=to, task_id=task_id, in_reply_to=in_reply_to,
        status=status, from_session=session)
    return {"ok": True, "deduped": deduped, "message": msg}


async def room_inbox(
    ctx: Context, room: Room = "main", consumer: Consumer = messages.DEFAULT_CONSUMER,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    since_id: Annotated[int | None, Field(ge=0)] = None,
) -> dict[str, Any]:
    """Чужие сообщения мне или всем после курсора потребителя. Курсор НЕ двигается: после обработки вызови room_ack. Разные runtime одного агента — разные consumer. Unread messages; confirm with room_ack after handling."""
    me = client_of(ctx)
    items, cursor = await messages.inbox(agent=me, room=_room(room), consumer=consumer,
                                         limit=limit, since_id=since_id)
    return {"agent": me, "consumer": consumer, "room": room, "messages": items,
            "cursor": cursor, "more": len(items) == limit}


async def room_ack(
    up_to_id: Annotated[int, Field(ge=1)], ctx: Context, room: Room = "main",
    consumer: Consumer = messages.DEFAULT_CONSUMER,
) -> dict[str, Any]:
    """Подтвердить обработку сообщений до up_to_id включительно (курсор только растёт). Acknowledge handled messages."""
    cursor = await messages.ack(agent=client_of(ctx), room=_room(room), consumer=consumer,
                                up_to_id=up_to_id)
    return {"ok": True, "consumer": consumer, "cursor": cursor}


async def room_history(
    room: Room = "main", limit: Annotated[int, Field(ge=1, le=200)] = 50,
    before_id: Annotated[int | None, Field(ge=1)] = None, task_id: Ident | None = None,
) -> dict[str, Any]:
    """Вся переписка комнаты (включая адресные сообщения), по возрастанию id. Full room transcript, oldest first."""
    return {"room": room, "messages": await messages.history(
        room=_room(room), limit=limit, before_id=before_id, task_id=task_id)}


async def room_task_open(
    task_id: Ident, ctx: Context, room: Room = "main",
    title: Annotated[str | None, Field(max_length=500)] = None, paths: Paths | None = None,
) -> dict[str, Any]:
    """Завести задачу без захвата (чтобы её мог взять другой агент). Create an unclaimed task."""
    task, created = await tasks.open_task(room=_room(room), task_id=task_id,
                                          agent=client_of(ctx), title=title, paths=paths)
    return {"ok": True, "created": created, "task": task}


async def room_task_claim(
    task_id: Ident, ctx: Context, room: Room = "main",
    lease_seconds: Annotated[int, Field(ge=60, le=MAX_LEASE_S)] = DEFAULT_LEASE_S,
    title: Annotated[str | None, Field(max_length=500)] = None, paths: Paths | None = None,
) -> dict[str, Any]:
    """Захватить задачу в аренду; верни fencing_token во все последующие правки. Повтор своим агентом продлевает аренду. Claim a task lease; pass the returned fencing_token to update/release."""
    task = await tasks.claim(room=_room(room), task_id=task_id, agent=client_of(ctx),
                             lease_seconds=lease_seconds, title=title, paths=paths)
    return {"ok": True, "task": task}


async def room_task_update(
    task_id: Ident, fencing_token: Annotated[int, Field(ge=1)], ctx: Context,
    room: Room = "main", status: Literal["in_progress", "blocked"] | None = None,
    note: Annotated[str | None, Field(max_length=4000)] = None,
    extend_seconds: Annotated[int | None, Field(ge=60, le=MAX_LEASE_S)] = None,
) -> dict[str, Any]:
    """Обновить свою задачу (статус/заметка/продление аренды); устаревший fencing_token отвергается. Update a task you hold."""
    task = await tasks.update(room=_room(room), task_id=task_id, agent=client_of(ctx),
                              fencing_token=fencing_token, status=status, note=note,
                              extend_seconds=extend_seconds)
    return {"ok": True, "task": task}


async def room_task_release(
    task_id: Ident, fencing_token: Annotated[int, Field(ge=1)], ctx: Context,
    room: Room = "main", status: Literal["open", "done", "blocked"] = "done",
    note: Annotated[str | None, Field(max_length=4000)] = None,
) -> dict[str, Any]:
    """Отпустить задачу: done — сделано, blocked — упёрлись, open — вернуть в общий пул. Release a task you hold."""
    task = await tasks.release(room=_room(room), task_id=task_id, agent=client_of(ctx),
                               fencing_token=fencing_token, status=status, note=note)
    return {"ok": True, "task": task}


async def room_tasks(
    room: Room = "main",
    status: Literal["open", "in_progress", "blocked", "done"] | None = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Список задач комнаты со статусом и живым держателем аренды. List room tasks."""
    return {"room": room, "tasks": await tasks.list_tasks(room=_room(room), status=status,
                                                          limit=limit)}


ROOM_TOOLS = (room_post, room_inbox, room_ack, room_history, room_task_open,
              room_task_claim, room_task_update, room_task_release, room_tasks)
