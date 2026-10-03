"""MCP-инструменты записи. Каждая запись попадает в `mcp_audit` и откатывается `undo`.

Событие скрывается (`hidden`), связь снимается (`is_current=false`), прежние
версии лежат в журнале. Слияние сущностей (`entity_merge`) — исключение: оно
удаляет строки-дубли, но кладёт в журнал весь `MergeReport`, и
`entity_unmerge`/`undo` возвращает их с прежними id.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession
from vera_shared.db.engine import get_session
from vera_shared.events import edit as event_edit
from vera_shared.graph import edit as graph_edit
from vera_shared.graph.merge import merge_entities
from vera_shared.memory.remember import RememberOutcome, remember_fact

from vera_mcp import audit
from vera_mcp.auth import client_of
from vera_mcp.merge_guard import MergeBlocked, entity_names, merge_blockers
from vera_mcp.undo import undo_entry

#: (target_id, before, after, extra-поля ответа)
Applied = tuple[int | None, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]
MAX_CONTENT_CHARS = 50_000


async def _audited(ctx: Context, tool: str, args: dict[str, Any], kind: str,
                   op: Callable[[AsyncSession], Awaitable[Applied]]) -> dict[str, Any]:
    """Правка и запись журнала — одна транзакция."""
    async with get_session() as s:
        target_id, before, after, extra = await op(s)
        audit_id = await audit.record(
            s, client=client_of(ctx), tool=tool, args=args, kind=kind,
            target_id=target_id, before=before, after=after)
    return {"ok": True, "audit_id": audit_id, **extra}


async def remember(
    text: Annotated[str, Field(min_length=3, max_length=8000)],
    ctx: Context,
    kind: Literal["fact", "decision", "todo", "preference"] = "fact",
    context: Annotated[str | None, Field(max_length=2000)] = None,
    tags: Annotated[list[str] | None, Field(max_length=10)] = None,
) -> dict[str, Any]:
    """Запомнить факт/решение/задачу/предпочтение (с дедупом; audit_id=null при точном дубле — ничего не создано). Remember a self-contained fact; audit_id is null for an exact duplicate."""
    result: dict[str, Any] = {"ok": True, "audit_id": None}
    args = {"text": text, "kind": kind, "context": context, "tags": tags}

    async def journal(s: AsyncSession, outcome: RememberOutcome) -> None:
        # та же транзакция, что и вставка события (см. remember_fact)
        result["audit_id"] = await audit.record(
            s, client=client_of(ctx), tool="remember", args=args, kind="event",
            target_id=outcome.event_id, before=None,
            after={"content_text": text.strip(), "deduped": outcome.deduped})

    outcome = await remember_fact(text, kind, context, tags, on_written=journal)
    return {**result, "event_id": outcome.event_id, "deduped": outcome.deduped,
            "dedup_reason": outcome.dedup_reason, "unhidden": outcome.unhidden,
            "similar_event_id": outcome.similar_event_id}


async def update_event(
    event_id: int, ctx: Context,
    content_text: Annotated[str | None, Field(min_length=1, max_length=MAX_CONTENT_CHARS)] = None,
    metadata: dict[str, Any] | None = None,
    category: Annotated[str | None, Field(min_length=1, max_length=50)] = None,
) -> dict[str, Any]:
    """Изменить текст/метаданные/категорию события; метаданные сливаются по ключам (null удаляет ключ); правка текста пере-индексирует событие; события в обработке (processing/media_pending) не правятся. Edit an event of ANY source; the previous version is kept in the audit log."""
    if content_text is None and not metadata and category is None:
        raise ValueError("nothing to change: pass content_text, metadata or category")

    async def op(s: AsyncSession) -> Applied:
        before, after = await event_edit.update_event(
            s, event_id, content_text=content_text, metadata_patch=metadata,
            category=category)
        return event_id, before, after, {"requeued_for_embedding":
                                         after["triage_status"] == "pending"}

    return await _audited(ctx, "update_event",
                          {"event_id": event_id, "content_text": content_text,
                           "metadata": metadata, "category": category}, "event", op)


async def hide_event(event_id: int, ctx: Context) -> dict[str, Any]:
    """Скрыть событие из поиска и выдач (не удаляя). Soft-hide an event; reversible via unhide_event / undo."""
    async def op(s: AsyncSession) -> Applied:
        before, after = await event_edit.set_hidden(s, event_id, hidden=True)
        return event_id, before, after, {"hidden": True}

    return await _audited(ctx, "hide_event", {"event_id": event_id}, "event", op)


async def unhide_event(event_id: int, ctx: Context) -> dict[str, Any]:
    """Вернуть скрытое событие в поиск. Restore a hidden event to search."""
    async def op(s: AsyncSession) -> Applied:
        before, after = await event_edit.set_hidden(s, event_id, hidden=False)
        return event_id, before, after, {"hidden": False}

    return await _audited(ctx, "unhide_event", {"event_id": event_id}, "event", op)


async def entity_rename(
    entity_id: int, name: Annotated[str, Field(min_length=1, max_length=500)],
    ctx: Context,
) -> dict[str, Any]:
    """Переименовать сущность. Rename an entity (old name kept in the audit log)."""
    async def op(s: AsyncSession) -> Applied:
        before, after = await graph_edit.rename_entity(s, entity_id, name)
        return entity_id, before, after, {"name": name}

    return await _audited(ctx, "entity_rename", {"entity_id": entity_id, "name": name},
                          "entity", op)


async def entity_add_alias(
    entity_id: int, source: Annotated[str, Field(min_length=1, max_length=40)],
    identifier: Annotated[str, Field(min_length=1, max_length=500)], ctx: Context,
    display_name: Annotated[str | None, Field(max_length=500)] = None,
) -> dict[str, Any]:
    """Добавить сущности алиас (source + identifier, напр. telegram + user:123 или gmail + адрес). Attach an identifier; refuses if it belongs to another entity."""
    args = {"entity_id": entity_id, "source": source, "identifier": identifier,
            "display_name": display_name}
    async with get_session() as s:
        alias_id, created = await graph_edit.add_alias(
            s, entity_id, source, identifier, display_name)
        if not created:
            return {"ok": True, "created": False, "alias_id": alias_id, "audit_id": None}
        audit_id = await audit.record(
            s, client=client_of(ctx), tool="entity_add_alias", args=args, kind="alias",
            target_id=alias_id, before=None, after={"entity_id": entity_id})
    return {"ok": True, "created": True, "alias_id": alias_id, "audit_id": audit_id}


async def relationship_set(
    subject_id: int, object_id: int, predicate: str, ctx: Context,
    fact: Annotated[str | None, Field(max_length=2000)] = None,
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.8,
) -> dict[str, Any]:
    """Создать или обновить связь (predicate из графа: boss_of, works_at, spouse_of …) и сделать её текущей. Add/update a relationship between two entities."""
    args = {"subject_id": subject_id, "object_id": object_id, "predicate": predicate,
            "fact": fact, "confidence": confidence}

    async def op(s: AsyncSession) -> Applied:
        rel_id, before, after = await graph_edit.set_relationship(
            s, subject_id, object_id, predicate, fact, confidence)
        return rel_id, before, after, {"relationship_id": rel_id, "created": before is None}

    return await _audited(ctx, "relationship_set", args, "relationship", op)


async def relationship_retire(relationship_id: int, ctx: Context) -> dict[str, Any]:
    """Снять связь (is_current=false), не удаляя. Retire a relationship; reversible via undo."""
    async def op(s: AsyncSession) -> Applied:
        before, after = await graph_edit.retire_relationship(s, relationship_id)
        return relationship_id, before, after, {"relationship_id": relationship_id}

    return await _audited(ctx, "relationship_retire",
                          {"relationship_id": relationship_id}, "relationship", op)


async def entity_merge(
    keep_id: int, drop_ids: Annotated[list[int], Field(min_length=1, max_length=20)],
    reason: Annotated[str, Field(min_length=3, max_length=500)], ctx: Context,
    dry_run: bool = True, force: bool = False,
) -> dict[str, Any]:
    """Слить дубли: drop_ids вливаются в keep_id. По умолчанию dry_run=true: возвращает имена и счётчики, ничего не меняя; выполнить — dry_run=false. Владелец и сущности с identity-узлами требуют force=true; откат — entity_unmerge/undo. Merge duplicate entities; dry run by default."""
    ids = [keep_id, *drop_ids]
    async with get_session() as s:
        blockers = await merge_blockers(s, ids)
    blocked = bool(blockers) and not force
    if dry_run:
        return await _merge_preview(keep_id, drop_ids, reason, blockers, blocked)
    if blocked:
        raise MergeBlocked("; ".join(blockers) + " — pass force=true to merge anyway")

    async def op(s: AsyncSession) -> Applied:
        report = await merge_entities(keep_id, drop_ids, reason, session=s)
        name = (await graph_edit.current_name(s, keep_id))["name"]
        return keep_id, report.to_dict(), {"name": name}, {
            "dry_run": False, "keep_id": keep_id, "merged": report.drop_ids,
            "counts": report.counts()}

    return await _audited(ctx, "entity_merge",
                          {"keep_id": keep_id, "drop_ids": drop_ids, "reason": reason,
                           "force": force}, "merge", op)


async def _merge_preview(keep_id: int, drop_ids: list[int], reason: str,
                         blockers: list[str], blocked: bool) -> dict[str, Any]:
    """Настоящее слияние в транзакции, которая откатывается: счётчики точные."""
    async with get_session() as s:
        names = await entity_names(s, [keep_id, *drop_ids])
        counts: dict[str, int] | None = None
        if not blocked:
            counts = (await merge_entities(keep_id, drop_ids, reason, session=s)).counts()
            await s.rollback()
    return {"ok": True, "dry_run": True, "keep": {"id": keep_id, "name": names.get(keep_id)},
            "drops": [{"id": i, "name": names.get(i)} for i in drop_ids],
            "counts": counts, "blockers": blockers, "would_be_refused": blocked,
            "audit_id": None}


async def entity_unmerge(merge_audit_id: int, ctx: Context,
                         force: bool = False) -> dict[str, Any]:
    """Разделить слияние обратно по audit_id записи entity_merge (то же, что undo, но только для слияний). Reverse an entity_merge by its audit id."""
    async with get_session() as s:
        entry = await audit.get_entry(s, merge_audit_id)
        if entry.tool != "entity_merge":
            raise ValueError(f"audit entry {merge_audit_id} is '{entry.tool}', not entity_merge")
        return await undo_entry(s, merge_audit_id, client_of(ctx), force)


async def undo(audit_id: int, ctx: Context, force: bool = False) -> dict[str, Any]:
    """Откатить запись журнала по id (remember скрывает событие). Undo an audit entry; refuses if the object changed since unless force."""
    async with get_session() as s:
        return await undo_entry(s, audit_id, client_of(ctx), force)


WRITE_TOOLS = (remember, update_event, hide_event, unhide_event, entity_rename,
               entity_add_alias, relationship_set, relationship_retire, entity_merge,
               entity_unmerge, undo)
