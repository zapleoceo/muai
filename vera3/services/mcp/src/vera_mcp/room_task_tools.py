"""MCP-инструменты задач комнаты. Автор и держатель — имя токена, не аргумент."""
from __future__ import annotations

import os
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from pydantic import Field
from vera_shared.room import (
    task_cancel,
    task_inventory,
    task_metadata,
    task_progress,
    task_queue,
    task_wait,
    tasks,
)

from vera_mcp.auth import client_of
from vera_mcp.room_common import (
    DEFAULT_LEASE_S,
    MAX_LEASE_S,
    Ident,
    Paths,
    Project,
    Room,
    _room,
    check_session,
)
from vera_mcp.room_redact import events_view, task_view, tasks_view


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
    return {"ok": True, "created": created, "task": task_view(task)}


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
                             session=check_session(client_of(ctx), session), account=account)
    return {"ok": True, "task": task_view(task)}


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
    return {"ok": True, "task": task_view(task)}


async def room_task_metadata_correct(
    task_id: Ident, expected_updated_at: Ident, reason: Annotated[str, Field(min_length=1, max_length=500)],
    ctx: Context, room: Room = "main",
    responsible: Annotated[str | None, Field(max_length=128)] = None,
    next_action: Annotated[str | None, Field(max_length=2000)] = None,
) -> dict[str, Any]:
    """Correct responsible/next_action without claiming or reporting progress; dot only, no active hold."""
    if os.environ.get("ROOM_METADATA_ENABLED", "").strip() != "1":
        raise ValueError("metadata correction is disabled")
    result = await task_metadata.correct_metadata(
        room=_room(room), task_id=task_id, agent=client_of(ctx),
        expected_updated_at=expected_updated_at, reason=reason,
        responsible=responsible, next_action=next_action)
    return {"ok": True, **result, "task": task_view(result["task"])}


async def room_task_progress(
    task_id: Ident, fencing_token: Annotated[int, Field(ge=1)], ctx: Context,
    result: Annotated[str, Field(min_length=1, max_length=4000)], room: Room = "main",
    next_checkpoint_seconds: Annotated[int | None, Field(ge=60, le=86_400)] = None,
) -> dict[str, Any]:
    """Содержательный прогресс (что сделано) — единственное, что двигает last_progress_at; продление аренды прогрессом не считается. next_checkpoint_seconds — когда ждать следующий отчёт. Report real progress, distinct from lease heartbeat."""
    task = await task_progress.progress(
        room=_room(room), task_id=task_id, agent=client_of(ctx), fencing_token=fencing_token,
        result=result, next_checkpoint_seconds=next_checkpoint_seconds)
    return {"ok": True, "task": task_view(task)}


async def room_task_state(
    task_id: Ident, fencing_token: Annotated[int, Field(ge=1)], ctx: Context,
    state: Literal["paused", "review", "resumed", "blocked", "unblocked"],
    reason: Annotated[str, Field(min_length=1, max_length=4000)], room: Room = "main",
) -> dict[str, Any]:
    """Сменить состояние: paused/review — только событие журнала, blocked/unblocked/resumed меняют статус (blocked/in_progress). Вопросы владельцу — отдельно. Record a task state change."""
    task = await task_progress.set_state(
        room=_room(room), task_id=task_id, agent=client_of(ctx), fencing_token=fencing_token,
        state=state, reason=reason)
    return {"ok": True, "task": task_view(task)}


async def room_task_wait(
    task_id: Ident, fencing_token: Annotated[int, Field(ge=1)], ctx: Context,
    until_seconds: Annotated[int, Field(ge=60, le=604_800)],
    reason: Annotated[str, Field(min_length=1, max_length=4000)], room: Room = "main",
    decision_owner: Annotated[str | None, Field(max_length=64)] = None,
) -> dict[str, Any]:
    """Законно ждать внешнее событие до срока (until_seconds от сейчас): до срока задача — waiting, после — stale_progress. Любой room_task_progress ожидание снимает. Wait for an external event with a deadline."""
    task = await task_wait.wait(
        room=_room(room), task_id=task_id, agent=client_of(ctx), fencing_token=fencing_token,
        until_seconds=until_seconds, reason=reason, decision_owner=decision_owner)
    return {"ok": True, "task": task_view(task)}


async def room_task_next(
    ctx: Context, room: Room = "main", project: Project | None = None,
    lease_seconds: Annotated[int, Field(ge=60, le=MAX_LEASE_S)] = DEFAULT_LEASE_S,
    session: Annotated[str | None, Field(max_length=128)] = None,
    account: Annotated[str | None, Field(max_length=64)] = None,
) -> dict[str, Any]:
    """Атомарно взять ОДНУ следующую задачу: open, без живой аренды, auto_pickup, все depends_on done, нет открытого вопроса владельцу, не на паузе; сперва priority (0 срочнее), затем created_at. task=null, если подходящей нет. Claim the next eligible task."""
    task = await task_queue.next_task(
        room=_room(room), agent=client_of(ctx), project=project, lease_seconds=lease_seconds,
        session=check_session(client_of(ctx), session), account=account)
    return {"ok": True, "task": task_view(task)}


async def room_task_history(
    task_id: Ident, room: Room = "main", since_id: Annotated[int | None, Field(ge=0)] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 100,
) -> dict[str, Any]:
    """Журнал событий задачи по возрастанию id (только чтение, после since_id). Task event log, oldest first."""
    return {"room": room, "task_id": task_id, "events": events_view(await task_progress.history(
        room=_room(room), task_id=task_id, since_id=since_id, limit=limit))}


async def room_task_release(
    task_id: Ident, fencing_token: Annotated[int, Field(ge=1)], ctx: Context,
    room: Room = "main", status: Literal["open", "done", "blocked"] = "done",
    note: Annotated[str | None, Field(max_length=4000)] = None,
) -> dict[str, Any]:
    """Отпустить задачу: done — сделано, blocked — упёрлись, open — вернуть в общий пул. Release a task you hold."""
    task = await tasks.release(room=_room(room), task_id=task_id, agent=client_of(ctx),
                               fencing_token=fencing_token, status=status, note=note)
    return {"ok": True, "task": task_view(task)}


async def room_task_cancel(
    task_id: Ident, fencing_token: Annotated[int, Field(ge=0)], ctx: Context,
    reason: Annotated[str, Field(min_length=1, max_length=4000)], room: Room = "main",
) -> dict[str, Any]:
    """Отменить задачу без захвата: статус cancelled, причина в note и журнале, аренда снимается, остальные поля сохраняются. Может держатель живой аренды или создатель задачи без живой аренды; fencing_token — текущий сохранённый (у ни разу не захваченной 0), устаревший отвергается. done/cancelled отменить нельзя. Cancel a task without claiming it."""
    task = await task_cancel.cancel(room=_room(room), task_id=task_id, agent=client_of(ctx),
                                    fencing_token=fencing_token, reason=reason)
    return {"ok": True, "task": task_view(task)}


async def room_tasks(
    room: Room = "main",
    status: Literal["open", "in_progress", "blocked", "done", "cancelled"] | None = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    queue: Literal["open", "unclaimed"] | None = None,
) -> dict[str, Any]:
    """Список задач комнаты со статусом, живым держателем и attention {state, label, since, last_result_at}. queue=open — все незавершённые, queue=unclaimed — незавершённые без живой аренды. List room tasks."""
    return {"room": room, "tasks": tasks_view(await task_queue.list_tasks(
        room=_room(room), status=status, limit=limit, queue=queue))}


async def room_task_inventory(
    room: Room = "main", limit: Annotated[int, Field(ge=1, le=200)] = 100,
    cursor: Annotated[str | None, Field(max_length=1024)] = None,
) -> dict[str, Any]:
    """Read every Room task through a checked keyset cursor. Continue until
    complete=true; if inconsistent=true, restart from the first page. Read-only."""
    result = await task_inventory.page(room=_room(room), limit=limit, cursor=cursor)
    return {"room": room, **result, "tasks": tasks_view(result["tasks"])}
