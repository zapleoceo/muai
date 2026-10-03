"""Откат слияния по `MergeReport`.

Возвращает удалённые сущности с прежними id, двигает строки обратно по их
первичным ключам и восстанавливает строку keep. Строки, появившиеся у keep
ПОСЛЕ слияния, остаются у keep: по отчёту не определить, чьи они были.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.db.models_graph import (
    ConnectionSuppressionRow,
    EntityAliasRow,
    EntityAvatarRow,
    EntityRow,
    IdentityNodeRow,
    MembershipRow,
    MergeSuggestionRow,
    RelationshipRow,
)
from vera_shared.db.models_links import EntityNicknameRow, EventEntityRow, VoiceSpeakerMapRow
from vera_shared.graph.merge_codec import decode_values
from vera_shared.graph.merge_report import MergeReport

_TABLES: dict[str, type] = {
    "entity_aliases": EntityAliasRow,
    "memberships": MembershipRow,
    "relationships": RelationshipRow,
    "entity_avatars": EntityAvatarRow,
    "identity_nodes": IdentityNodeRow,
    "merge_suggestions": MergeSuggestionRow,
    "connection_suppressions": ConnectionSuppressionRow,
    "entity_nicknames": EntityNicknameRow,
    "voice_speaker_map": VoiceSpeakerMapRow,
    "event_entities": EventEntityRow,
}


class UnmergeError(RuntimeError):
    """Откат невозможен: id удалённой сущности уже занят или нет keep."""


async def _set(s: AsyncSession, table: str, pk: str, pk_value: Any,
               values: dict[str, Any]) -> None:
    cls = _TABLES[table]
    await s.execute(update(cls).where(getattr(cls, pk) == pk_value).values(**values))


async def _set_by_keys(s: AsyncSession, table: str, keys: dict[str, Any],
                       values: dict[str, Any]) -> None:
    cls = _TABLES[table]
    await s.execute(update(cls).where(*(getattr(cls, k) == v for k, v in keys.items()))
                    .values(**values))


async def _drop_pruned_event_link(s: AsyncSession, row: dict[str, Any]) -> None:
    """Событие, на которое ссылалась связь, могли вычистить с тех пор — тогда
    FK не даст вставить строку; в проде для такого случая стоит SET NULL."""
    event_id = row.get("derived_from_event_id")
    if event_id is None:
        return
    if (await s.execute(select(EventRow.id).where(EventRow.id == event_id))).first() is None:
        row["derived_from_event_id"] = None


async def _unmerge(s: AsyncSession, report: MergeReport) -> None:
    keep = await s.get(EntityRow, report.keep_id)
    if keep is None:
        raise UnmergeError(f"keep {report.keep_id} уже не существует")
    taken = (await s.execute(select(EntityRow.id).where(
        EntityRow.id.in_(report.drop_ids)))).scalars().all()
    if taken:
        raise UnmergeError(f"id {sorted(taken)} уже заняты — откат затёр бы чужое")

    for row in report.dropped:
        s.add(EntityRow(**decode_values(row)))
    await s.flush()

    # Все колонки одной строки — одним UPDATE: пошагово пара (entity_a, entity_b)
    # у предложения на миг совпала бы с чужой и нарушила uq_merge_pair.
    grouped: dict[tuple[str, str, Any], dict[str, Any]] = {}
    for move in reversed(report.moved):
        if "keys" in move:                       # таблица с составным ключом
            await _set_by_keys(s, move["table"], move["keys"], {move["column"]: move["old"]})
            continue
        grouped.setdefault((move["table"], move["pk"], move["pk_value"]), {})[
            move["column"]] = move["old"]
    for (table, pk, pk_value), values in grouped.items():
        await _set(s, table, pk, pk_value, values)
    for made in report.created:                  # созданное слиянием удаляется до возврата удалённого
        cls = _TABLES[made["table"]]
        await s.execute(delete(cls).where(*(getattr(cls, k) == v for k, v in made["keys"].items())))
    for gone in reversed(report.deleted):
        row = decode_values(gone["row"])
        if gone["table"] == "relationships":
            await _drop_pruned_event_link(s, row)
        s.add(_TABLES[gone["table"]](**row))
        await s.flush()
    for upd in report.updated:
        await _set(s, upd["table"], upd["pk"], upd["pk_value"],
                   decode_values(upd["before"]))

    before = decode_values(report.keep_before)
    for column in ("name", "type", "canonical_id", "attributes",
                   "first_seen_at", "last_seen_at"):
        setattr(keep, column, before[column])
    await s.flush()


async def unmerge(report: MergeReport | dict[str, Any], *,
                  session: AsyncSession | None = None) -> None:
    rep = report if isinstance(report, MergeReport) else MergeReport.from_dict(report)
    if session is not None:
        await _unmerge(session, rep)
        return
    async with get_session() as s:
        await _unmerge(s, rep)
