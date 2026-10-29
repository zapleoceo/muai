"""MCP-инструменты сообщений комнаты агентов. Автор — имя токена, не аргумент."""
from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from pydantic import Field
from vera_shared.room import messages, task_queue

from vera_mcp.auth import client_of
from vera_mcp.room_common import (  # noqa: F401
    DEFAULT_LEASE_S,
    MAX_BODY_CHARS,
    MAX_LEASE_S,
    Agent,
    Consumer,
    Ident,
    Paths,
    Project,
    Room,
    _room,
    check_session,
)
from vera_mcp.room_oauth import configured_agents
from vera_mcp.room_redact import message_view, messages_view, tasks_view
from vera_mcp.room_task_tools import (  # noqa: F401
    room_task_cancel,
    room_task_claim,
    room_task_history,
    room_task_inventory,
    room_task_metadata_correct,
    room_task_next,
    room_task_open,
    room_task_progress,
    room_task_release,
    room_task_state,
    room_task_update,
    room_task_wait,
    room_tasks,
)


async def room_post(
    body: Annotated[str, Field(min_length=1, max_length=MAX_BODY_CHARS)], ctx: Context,
    room: Room = "main", to: Agent | None = None, task_id: Ident | None = None,
    in_reply_to: Ident | int | None = None,
    status: Literal["info", "request", "ack", "question", "in_progress", "done",
                    "blocked"] = "info",
    message_id: Ident | None = None, session: Ident | None = None,
) -> dict[str, Any]:
    """Написать в комнату (to=None — всем; to — адресация, не приватность). Повтор message_id с тем же содержимым — не дубль, с другим — ошибка. session — метка твоего runtime для аудита. Post a message; the same message_id is idempotent."""
    me = client_of(ctx)
    reply_id = in_reply_to
    if isinstance(in_reply_to, int):
        prior = await messages.get_message(room=_room(room), id=in_reply_to)
        if prior is None:
            raise ValueError(f"in_reply_to message {in_reply_to} not found in room {room}")
        reply_id = prior["message_id"]
    msg, deduped = await messages.post_message(
        room=_room(room), message_id=message_id or f"{me}-{uuid.uuid4().hex[:12]}",
        from_agent=me, body=body, to_agent=to, task_id=task_id,
        in_reply_to=reply_id,
        status=status, from_session=check_session(me, session))
    return {"ok": True, "deduped": deduped, "message": message_view(msg)}


async def room_inbox(
    ctx: Context, room: Room = "main", consumer: Consumer = messages.DEFAULT_CONSUMER,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    since_id: Annotated[int | None, Field(ge=0)] = None,
    after_id: Annotated[int | None, Field(ge=0)] = None,
    session: Ident | None = None,
    to: Agent | None = None, kind: str | None = None,
    exclude_system: bool = False, task_id: Ident | None = None,
    max_body_chars: Annotated[int, Field(ge=0, le=MAX_BODY_CHARS)] = 0,
) -> dict[str, Any]:
    """Unread Room messages. Legacy calls keep full bodies. Compact reads need
    room_message_get for full text. next_cursor is safe only for unfiltered,
    untruncated pages and never moves the stored cursor; ack after processing."""
    me = client_of(ctx)
    items, cursor = await messages.inbox(agent=me, room=_room(room), consumer=consumer,
                                         limit=limit, since_id=since_id, after_id=after_id,
                                         to=to, kind=kind, exclude_system=exclude_system,
                                         task_id=task_id, session=session,
                                         reserved=configured_agents())
    mine = await task_queue.held_by(room=_room(room), agent=me)
    viewed = messages_view(items, max_body_chars)
    safe_cursor = (since_id is None and after_id is None and to is None and
                   kind is None and task_id is None and not exclude_system and
                   not any(item["truncated"] for item in viewed))
    return {"agent": me, "consumer": consumer, "room": room,
            "messages": viewed,
            "cursor": cursor,
            "next_cursor": (items[-1]["id"] if items else cursor) if safe_cursor else None,
            "more": len(items) == limit,
            "my_tasks": tasks_view([{"task_id": t["task_id"], "title": t["title"],
                                     "attention": t["attention"]} for t in mine])}


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
    after_id: Annotated[int | None, Field(ge=0)] = None,
    to: Agent | None = None, kind: str | None = None,
    exclude_system: bool | None = None,
    max_body_chars: Annotated[int, Field(ge=0, le=MAX_BODY_CHARS)] = 0,
    compact: bool = False,
) -> dict[str, Any]:
    """Вся переписка комнаты (включая адресные сообщения), по возрастанию id. Full room transcript, oldest first."""
    if compact:
        max_body_chars = max_body_chars or 500
        if exclude_system is None:
            exclude_system = True
    items = await messages.history(
        room=_room(room), limit=limit, before_id=before_id, after_id=after_id,
        task_id=task_id, to=to, kind=kind, exclude_system=bool(exclude_system))
    return {"room": room, "messages": messages_view(items, max_body_chars)}


async def room_message_get(
    id: Annotated[int, Field(ge=1)], room: Room = "main", task_id: Ident | None = None,
) -> dict[str, Any]:
    """Fetch full text by numeric id; optional task_id rejects an unrelated result."""
    item = await messages.get_message(room=_room(room), id=id, task_id=task_id)
    if item is None:
        raise ValueError(f"message {id} not found in room {room}")
    return {"room": room, "message": message_view(item)}


ROOM_TOOLS = (room_post, room_inbox, room_ack, room_history,
              room_task_open, room_task_claim, room_task_update, room_task_progress,
              room_task_state,
              room_task_history, room_task_wait, room_task_next, room_task_release,
              room_tasks, room_message_get, room_task_cancel, room_task_inventory)
