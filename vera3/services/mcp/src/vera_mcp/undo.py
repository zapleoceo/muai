"""Откат записи журнала: по `before` возвращает состояние, ничего не удаляя.

Если с момента правки объект изменился (текущее состояние ≠ `after`),
откат отказывает без `force=True`, чтобы не затереть чужую свежую правку.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from vera_shared.db.models_mcp import McpAuditRow
from vera_shared.events import edit as event_edit
from vera_shared.graph import edit as graph_edit

from vera_mcp import audit

#: какие поля снимка сверяются с `after` перед откатом (триаж меняет статус
#: сам, поэтому для текстовой правки статус не сверяется)
_EVENT_CHECK_KEYS = {
    "update_event": ("content_text", "metadata", "category"),
    "hide_event": ("triage_status",),
}


class UndoRefused(ValueError):
    """Откат невозможен или небезопасен; сообщение объясняет почему."""


def _diverged(current: dict[str, Any], expected: dict[str, Any],
              keys: tuple[str, ...] | None) -> bool:
    check = keys or tuple(expected)
    return any(current.get(k) != expected.get(k) for k in check)


async def _undo_event(s: AsyncSession, row: McpAuditRow,
                      force: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    event_id = int(row.target_id or 0)
    if row.tool == "remember":
        return await event_edit.set_hidden(s, event_id, hidden=True)
    current = event_edit.snapshot(await event_edit.load_row(s, event_id))
    keys = _EVENT_CHECK_KEYS.get(row.tool)
    if keys is not None and not force and _diverged(current, row.after or {}, keys):
        raise UndoRefused("event changed since this edit; pass force=true to override")
    return current, await event_edit.restore_event(s, event_id, row.before or {})


async def _undo_entity(s: AsyncSession, row: McpAuditRow,
                       force: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    entity_id = int(row.target_id or 0)
    current = await graph_edit.current_name(s, entity_id)
    if not force and current != row.after:
        raise UndoRefused("entity was renamed again; pass force=true to override")
    return await graph_edit.rename_entity(s, entity_id, (row.before or {})["name"])


async def _undo_alias(s: AsyncSession, row: McpAuditRow,
                      force: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    removed = await graph_edit.remove_alias(s, int(row.target_id or 0))
    return {"exists": removed}, {"exists": False}


async def _undo_relationship(s: AsyncSession, row: McpAuditRow,
                             force: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    rel_id = int(row.target_id or 0)
    current = await graph_edit.current_relationship(s, rel_id)
    if not force and current != row.after:
        raise UndoRefused("relationship changed since this edit; pass force=true to override")
    if row.before is None:
        retired, after = await graph_edit.retire_relationship(s, rel_id)
        return retired, after
    return current, await graph_edit.restore_relationship(s, rel_id, row.before)


_UNDO_BY_KIND = {
    "event": _undo_event,
    "entity": _undo_entity,
    "alias": _undo_alias,
    "relationship": _undo_relationship,
}


async def undo_entry(s: AsyncSession, audit_id: int, client: str,
                     force: bool) -> dict[str, Any]:
    row = await audit.get_entry(s, audit_id)
    if row.undo_of is not None:
        raise UndoRefused("this entry is itself an undo; repeat the original change instead")
    if row.status == "undone":
        raise UndoRefused("already undone")
    handler = _UNDO_BY_KIND.get(row.target_kind)
    if handler is None:
        raise UndoRefused(f"cannot undo target kind '{row.target_kind}'")
    before, after = await handler(s, row, force)
    row.status = "undone"
    undo_id = await audit.record(
        s, client=client, tool="undo", args={"audit_id": audit_id, "force": force},
        kind=row.target_kind, target_id=row.target_id, before=before, after=after,
        undo_of=audit_id)
    return {"ok": True, "audit_id": undo_id, "undone": audit_id,
            "target": f"{row.target_kind}:{row.target_id}"}
