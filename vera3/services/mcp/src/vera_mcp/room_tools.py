"""MCP-инструменты комнаты агентов. Автор и держатель — имя токена, не аргумент."""
from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context
from pydantic import Field
from vera_shared.room import messages, task_progress, task_queue, task_wait, tasks

from vera_mcp.auth import allowed_rooms, client_of

Room = Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,64}$")]
Ident = Annotated[str, Field(min_length=1, max_length=128)]
Agent = Annotated[str, Field(min_length=1, max_length=64)]
Consumer = Annotated[str, Field(pattern=r"^[a-z0-9_.-]{1,64}$")]
Project = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
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
    """Чужие сообщения мне или всем после курсора потребителя. Курсор НЕ двигается: после обработки вызови room_ack. Разные runtime одного агента — разные consumer. Unread messages; confirm with room_ack after handling. my_tasks — мои незавершённые задачи с attention {state, label, since, last_result_at}."""
    me = client_of(ctx)
    items, cursor = await messages.inbox(agent=me, room=_room(room), consumer=consumer,
                                         limit=limit, since_id=since_id)
    mine = await task_queue.held_by(room=_room(room), agent=me)
    return {"agent": me, "consumer": consumer, "room": room, "messages": items,
            "cursor": cursor, "more": len(items) == limit,
            "my_tasks": [{"task_id": t["task_id"], "title": t["title"],
                          "attention": t["attention"]} for t in mine]}


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
    project: Project | None = None, priority: Annotated[int | None, Field(ge=0, le=3)] = None,
    auto_pickup: bool | None = None,
    depends_on: Annotated[list[Ident] | None, Field(max_length=20)] = None,
    next_action: Annotated[str | None, Field(max_length=2000)] = None,
    refs: Annotated[list[dict[str, Any]] | None, Field(max_length=20)] = None,
    responsible: Annotated[str | None, Field(max_length=128)] = None,
) -> dict[str, Any]:
    """Завести задачу без захвата (чтобы её мог взять другой агент); project/priority/auto_pickup/depends_on/next_action/refs/responsible (метка ответственного) — поля очереди, проверяются как в room_task_update, у существующей задачи не меняются. Create an unclaimed task."""
    task, created = await tasks.open_task(
        room=_room(room), task_id=task_id, agent=client_of(ctx), title=title, paths=paths,
        project=project, priority=priority, auto_pickup=auto_pickup, depends_on=depends_on,
        next_action=next_action, refs=refs, responsible=responsible)
    return {"ok": True, "created": created, "task": task}


async def room_task_claim(
    task_id: Ident, ctx: Context, room: Room = "main",
    lease_seconds: Annotated[int, Field(ge=60, le=MAX_LEASE_S)] = DEFAULT_LEASE_S,
    title: Annotated[str | None, Field(max_length=500)] = None, paths: Paths | None = None,
    session: Annotated[str | None, Field(max_length=128)] = None,
    account: Annotated[str | None, Field(max_length=64)] = None,
) -> dict[str, Any]:
    """Захватить задачу в аренду; верни fencing_token во все последующие правки. Повтор своим агентом продлевает аренду. Claim a task lease; pass the returned fencing_token to update/release."""
    task = await tasks.claim(room=_room(room), task_id=task_id, agent=client_of(ctx),
                             lease_seconds=lease_seconds, title=title, paths=paths,
                             session=session, account=account)
    return {"ok": True, "task": task}


async def room_task_update(
    task_id: Ident, fencing_token: Annotated[int, Field(ge=1)], ctx: Context,
    room: Room = "main", status: Literal["in_progress", "blocked"] | None = None,
    note: Annotated[str | None, Field(max_length=4000)] = None,
    extend_seconds: Annotated[int | None, Field(ge=60, le=MAX_LEASE_S)] = None,
    next_action: Annotated[str | None, Field(max_length=2000)] = None,
    priority: Annotated[int | None, Field(ge=0, le=3)] = None,
    refs: Annotated[list[dict[str, Any]] | None, Field(max_length=20)] = None,
    project: Project | None = None,
    depends_on: Annotated[list[Ident] | None, Field(max_length=20)] = None,
    auto_pickup: bool | None = None,
    responsible: Annotated[str | None, Field(max_length=128)] = None,
) -> dict[str, Any]:
    """responsible — метка ответственного (пустая = не менять, очистить нельзя, только заменить). project/depends_on [task_id, не сама на себя]/auto_pickup — поля очереди для room_task_next. Обновить свою задачу (статус/заметка/продление аренды/next_action/priority 0..3, 0 срочнее/refs [{kind: jira|url|event|chunk, ref, excerpt<=300}] — только указатели); устаревший fencing_token отвергается. Update a task you hold."""
    task = await tasks.update(room=_room(room), task_id=task_id, agent=client_of(ctx),
                              fencing_token=fencing_token, status=status, note=note,
                              extend_seconds=extend_seconds, next_action=next_action,
                              priority=priority, refs=refs, project=project,
                              depends_on=depends_on, auto_pickup=auto_pickup,
                              responsible=responsible)
    return {"ok": True, "task": task}


async def room_task_progress(
    task_id: Ident, fencing_token: Annotated[int, Field(ge=1)], ctx: Context,
    result: Annotated[str, Field(min_length=1, max_length=4000)], room: Room = "main",
    next_checkpoint_seconds: Annotated[int | None, Field(ge=60, le=86_400)] = None,
) -> dict[str, Any]:
    """Содержательный прогресс (что сделано) — единственное, что двигает last_progress_at; продление аренды прогрессом не считается. next_checkpoint_seconds — когда ждать следующий отчёт. Report real progress, distinct from lease heartbeat."""
    task = await task_progress.progress(
        room=_room(room), task_id=task_id, agent=client_of(ctx), fencing_token=fencing_token,
        result=result, next_checkpoint_seconds=next_checkpoint_seconds)
    return {"ok": True, "task": task}


async def room_task_state(
    task_id: Ident, fencing_token: Annotated[int, Field(ge=1)], ctx: Context,
    state: Literal["paused", "review", "resumed", "blocked", "unblocked"],
    reason: Annotated[str, Field(min_length=1, max_length=4000)], room: Room = "main",
) -> dict[str, Any]:
    """Сменить состояние: paused/review — только событие журнала, blocked/unblocked/resumed меняют статус (blocked/in_progress). Вопросы владельцу — отдельно. Record a task state change."""
    task = await task_progress.set_state(
        room=_room(room), task_id=task_id, agent=client_of(ctx), fencing_token=fencing_token,
        state=state, reason=reason)
    return {"ok": True, "task": task}


async def room_task_wait(
    task_id: Ident, fencing_token: Annotated[int, Field(ge=1)], ctx: Context,
    until_seconds: Annotated[int, Field(ge=60, le=604_800)],
    reason: Annotated[str, Field(min_length=1, max_length=4000)], room: Room = "main",
) -> dict[str, Any]:
    """Законно ждать внешнее событие до срока (until_seconds от сейчас): до срока задача — waiting, после — stale_progress. Любой room_task_progress ожидание снимает. Wait for an external event with a deadline."""
    task = await task_wait.wait(
        room=_room(room), task_id=task_id, agent=client_of(ctx), fencing_token=fencing_token,
        until_seconds=until_seconds, reason=reason)
    return {"ok": True, "task": task}


async def room_task_next(
    ctx: Context, room: Room = "main", project: Project | None = None,
    lease_seconds: Annotated[int, Field(ge=60, le=MAX_LEASE_S)] = DEFAULT_LEASE_S,
    session: Annotated[str | None, Field(max_length=128)] = None,
    account: Annotated[str | None, Field(max_length=64)] = None,
) -> dict[str, Any]:
    """Атомарно взять ОДНУ следующую задачу: open, без живой аренды, auto_pickup, все depends_on done, нет открытого вопроса владельцу, не на паузе; сперва priority (0 срочнее), затем created_at. task=null, если подходящей нет. Claim the next eligible task."""
    task = await task_queue.next_task(
        room=_room(room), agent=client_of(ctx), project=project, lease_seconds=lease_seconds,
        session=session, account=account)
    return {"ok": True, "task": task}


async def room_task_history(
    task_id: Ident, room: Room = "main", since_id: Annotated[int | None, Field(ge=0)] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 100,
) -> dict[str, Any]:
    """Журнал событий задачи по возрастанию id (только чтение, после since_id). Task event log, oldest first."""
    return {"room": room, "task_id": task_id, "events": await task_progress.history(
        room=_room(room), task_id=task_id, since_id=since_id, limit=limit)}


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
    queue: Literal["open", "unclaimed"] | None = None,
) -> dict[str, Any]:
    """Список задач комнаты со статусом, живым держателем и attention {state, label, since, last_result_at}. queue=open — все незавершённые, queue=unclaimed — незавершённые без живой аренды. List room tasks."""
    return {"room": room, "tasks": await task_queue.list_tasks(
        room=_room(room), status=status, limit=limit, queue=queue)}


ROOM_TOOLS = (room_post, room_inbox, room_ack, room_history, room_task_open,
              room_task_claim, room_task_update, room_task_progress, room_task_state,
              room_task_history, room_task_wait, room_task_next, room_task_release,
              room_tasks)
