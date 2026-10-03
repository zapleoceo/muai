"""Слияние сущностей для людей и агентов: предпросмотр без записи и слияние с журналом.

Один путь для MCP `entity_merge` и кнопок дашборда: те же проверки (`merge_guard`:
владелец и узлы личности требуют `force`), тот же отчёт `MergeReport` в `mcp_audit`
(`target_kind='merge'`), поэтому откат из `/journal` и через MCP `undo`/`entity_unmerge`
один и тот же. Предпросмотр — НАСТОЯЩЕЕ слияние в транзакции, которая откатывается:
счётчики точные, в базе не остаётся ничего.
"""
from __future__ import annotations

from typing import Any

from vera_shared.db.engine import get_session
from vera_shared.graph import edit as graph_edit
from vera_shared.graph.merge import merge_entities
from vera_shared.graph.merge_guard import MergeBlocked, entity_names, merge_blockers
from vera_shared.journal import audit


async def preview_merge(keep_id: int, drop_ids: list[int], reason: str,
                        force: bool = False) -> dict[str, Any]:
    async with get_session() as s:
        blockers = await merge_blockers(s, [keep_id, *drop_ids])
        blocked = bool(blockers) and not force
        names = await entity_names(s, [keep_id, *drop_ids])
        counts: dict[str, int] | None = None
        if not blocked:
            counts = (await merge_entities(keep_id, drop_ids, reason, session=s)).counts()
            await s.rollback()
    return {"ok": True, "dry_run": True, "keep": {"id": keep_id, "name": names.get(keep_id)},
            "drops": [{"id": i, "name": names.get(i)} for i in drop_ids],
            "counts": counts, "blockers": blockers, "would_be_refused": blocked,
            "audit_id": None}


async def apply_merge(keep_id: int, drop_ids: list[int], reason: str, client: str,
                      force: bool = False) -> dict[str, Any]:
    """Слияние и строка журнала одной транзакцией; `MergeBlocked` — нужен `force`."""
    async with get_session() as s:
        blockers = await merge_blockers(s, [keep_id, *drop_ids])
        if blockers and not force:
            raise MergeBlocked("; ".join(blockers) + " — pass force=true to merge anyway")
        report = await merge_entities(keep_id, drop_ids, reason, session=s)
        name = (await graph_edit.current_name(s, keep_id))["name"]
        audit_id = await audit.record(
            s, client=client, tool="entity_merge",
            args={"keep_id": keep_id, "drop_ids": drop_ids, "reason": reason, "force": force},
            kind="merge", target_id=keep_id, before=report.to_dict(), after={"name": name})
    return {"ok": True, "audit_id": audit_id, "dry_run": False, "keep_id": keep_id,
            "merged": report.drop_ids, "counts": report.counts()}
