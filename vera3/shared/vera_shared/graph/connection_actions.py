"""Разрыв неверной связи пары: гасим записанные роли или отвергаем выведенную.

Тот же путь, что у MCP (`graph.edit.retire_relationship` + запись в `mcp_audit`),
поэтому `journal.undo` откатывает такую правку как любую другую. Правка и строка
журнала — одна транзакция; клиент в журнале называет, кто правил (`dashboard`).
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.graph import edit as graph_edit
from vera_shared.graph.edit import GraphEditError
from vera_shared.graph.pair_stats import ordered
from vera_shared.graph.rel_canon import SYMMETRIC, canonical_edge
from vera_shared.graph.suppressions import suppress_pair
from vera_shared.journal import audit

MAX_RELS_PER_BREAK = 20


async def _check_belongs(s: AsyncSession, rel_id: int, pair: tuple[int, int],
                         predicate: str) -> tuple[str, int | None]:
    """Роль записи (канонический предикат, кто «над»); отказ, если запись чужая,
    погашена или это другая роль, чем просил вызывающий."""
    snap = await graph_edit.current_relationship(s, rel_id)
    ends = ordered(snap["subject_entity_id"], snap["object_entity_id"])
    if ends != pair:
        raise GraphEditError(f"relationship {rel_id} does not connect entities {pair[0]} and {pair[1]}")
    if not snap["is_current"]:
        raise GraphEditError(f"relationship {rel_id} is already retired")
    subject, canon, _ = canonical_edge(snap["subject_entity_id"], snap["predicate"],
                                       snap["object_entity_id"])
    if canon != predicate:
        raise GraphEditError(f"relationship {rel_id} is '{canon}', not '{predicate}'")
    return canon, None if canon in SYMMETRIC else subject


async def break_role(a: int, b: int, predicate: str, rel_ids: list[int],
                     client: str) -> list[int]:
    """Гасит записи ОДНОЙ роли пары (предикат + сторона иерархии); возвращает id строк
    журнала (по одной на запись)."""
    ids = sorted(set(rel_ids))
    if not ids or len(ids) > MAX_RELS_PER_BREAK:
        raise GraphEditError(f"rel_ids must hold 1..{MAX_RELS_PER_BREAK} relationship ids")
    pair = ordered(a, b)
    audit_ids: list[int] = []
    async with get_session() as s:
        roles = {await _check_belongs(s, rel_id, pair, predicate) for rel_id in ids}
        if len(roles) != 1:
            raise GraphEditError("rel_ids belong to different roles of the pair")
        for rel_id in ids:
            before, after = await graph_edit.retire_relationship(s, rel_id)
            audit_ids.append(await audit.record(
                s, client=client, tool="relationship_retire",
                args={"relationship_id": rel_id}, kind="relationship",
                target_id=rel_id, before=before, after=after))
    return audit_ids


async def reject_inferred(a: int, b: int, client: str) -> int | None:
    """Отвергает выведенное «работает с» пары. None — уже было отвергнуто."""
    if a == b:
        raise GraphEditError("pair members must differ")
    low, high = ordered(a, b)
    async with get_session() as s:
        for entity_id in (low, high):
            await graph_edit.current_name(s, entity_id)
        if not await suppress_pair(s, low, high):
            return None
        return await audit.record(
            s, client=client, tool="connection_suppress",
            args={"entity_a": low, "entity_b": high}, kind="suppression",
            target_id=None, before=None, after={"entity_a": low, "entity_b": high})
