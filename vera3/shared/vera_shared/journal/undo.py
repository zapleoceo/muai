"""Откат записи журнала: возвращает ТОЛЬКО поля, которые менял этот инструмент.

Строка журнала и объект берутся `FOR UPDATE`. Если поля, изменённые
инструментом, с тех пор менялись (текущее значение ≠ `after`), откат
отказывает без `force=True`, чтобы не затереть чужую свежую правку; при
`force` восстанавливаются те же поля — остальные не трогаются никогда.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models_mcp import McpAuditRow
from vera_shared.events import edit as event_edit
from vera_shared.graph import edit as graph_edit
from vera_shared.graph.merge_errors import MergeError
from vera_shared.graph.merge_report import MergeReport
from vera_shared.graph.suppressions import lift_suppression
from vera_shared.graph.unmerge import UnmergeError, unmerge
from vera_shared.journal import audit

_TEXT_KEYS = ("content_text", "metadata", "category")
_STATUS_KEYS = ("triage_status", "triage_metadata")
#: инструмент → поля снимка события, которые он менял
_EVENT_KEYS = {"update_event": _TEXT_KEYS, "hide_event": _STATUS_KEYS,
               "unhide_event": _STATUS_KEYS}


class UndoRefused(ValueError):
    """Откат невозможен или небезопасен; сообщение объясняет почему."""


def _diverged(current: dict[str, Any], expected: dict[str, Any],
              keys: tuple[str, ...]) -> bool:
    return any(current.get(k) != expected.get(k) for k in keys)


def _refuse_if_diverged(current: dict[str, Any], expected: dict[str, Any],
                        keys: tuple[str, ...], what: str, force: bool) -> None:
    if not force and _diverged(current, expected, keys):
        raise UndoRefused(f"{what} changed since this edit; pass force=true to override")


async def _undo_event(s: AsyncSession, row: McpAuditRow,
                      force: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    event_id = int(row.target_id or 0)
    current = event_edit.snapshot(await event_edit.load_row(s, event_id))
    if row.tool == "remember":
        _refuse_if_diverged(current, row.after or {}, ("content_text",), "event", force)
        return await event_edit.set_hidden(s, event_id, hidden=True)
    keys = _EVENT_KEYS[row.tool]
    _refuse_if_diverged(current, row.after or {}, keys, "event", force)
    return await event_edit.restore_fields(s, event_id, row.before or {}, keys)


async def _undo_entity(s: AsyncSession, row: McpAuditRow,
                       force: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    entity_id = int(row.target_id or 0)
    current = await graph_edit.current_name(s, entity_id)
    _refuse_if_diverged(current, row.after or {}, ("name",), "entity", force)
    return await graph_edit.rename_entity(s, entity_id, (row.before or {})["name"])


async def _undo_alias(s: AsyncSession, row: McpAuditRow,
                      force: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    alias_id = int(row.target_id or 0)
    owner = await graph_edit.alias_owner(s, alias_id)
    if owner is not None and owner != (row.after or {}).get("entity_id") and not force:
        raise UndoRefused("alias was moved to another entity; pass force=true to override")
    removed = await graph_edit.remove_alias(s, alias_id)
    return {"exists": removed}, {"exists": False}


async def _undo_relationship(s: AsyncSession, row: McpAuditRow,
                             force: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    rel_id = int(row.target_id or 0)
    current = await graph_edit.current_relationship(s, rel_id)
    expected = row.after or {}
    _refuse_if_diverged(current, expected, tuple(expected), "relationship", force)
    if row.before is None:
        return await graph_edit.retire_relationship(s, rel_id)
    return current, await graph_edit.restore_relationship(s, rel_id, row.before)


async def _undo_merge(s: AsyncSession, row: McpAuditRow,
                      force: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """`before` — весь MergeReport; `after` — имя победителя на момент слияния."""
    report = MergeReport.from_dict(row.before or {})
    current = await graph_edit.current_name(s, report.keep_id)
    _refuse_if_diverged(current, row.after or {}, ("name",), "entity", force)
    try:
        await unmerge(report, session=s)
    except (UnmergeError, MergeError) as e:
        raise UndoRefused(str(e)) from e
    return {"dropped": report.drop_ids}, {"restored": report.drop_ids}


async def _undo_suppression(s: AsyncSession, row: McpAuditRow,
                            force: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    after = row.after or {}
    lifted = await lift_suppression(s, int(after["entity_a"]), int(after["entity_b"]))
    return {"suppressed": lifted}, {"suppressed": False}


_UNDO_BY_KIND = {
    "suppression": _undo_suppression,
    "merge": _undo_merge,
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
