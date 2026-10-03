"""«Связи не про этого человека»: перенос связей, которые держатся на упоминании имени.

Случай: чужой Telegram-аккаунт «Діма» получил связи, потому что извлекатель связал имя
из чужих сообщений с карточкой по совпадению имени. Объединять карточки нельзя (аккаунт
чужой), а связи на самом деле про другого человека. Переносимыми считаются связи, чьё
событие-источник написал НЕ сам этот человек (ни один его алиас не автор), то есть
доказательство — упоминание имени. Ручные связи (без события) и связи по его собственным
сообщениям не трогаются.

Перенос меняет концы записи (`graph.edit.repoint_relationship`), каждая правка — строка
`mcp_audit` (`relationship_move`, при петле или дубле — `relationship_retire`), откат общий.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

from vera_shared.db.engine import get_session
from vera_shared.graph import edit as graph_edit
from vera_shared.graph.connection_data import entity_cards
from vera_shared.graph.edit import GraphEditError
from vera_shared.journal import audit

MAX_MOVED = 200
_SENDER_SOURCES = ("telegram", "slack", "instagram")


def authored_by(aliases: list[tuple[str, str]], source: str, sender_id: str | None,
                sender: str | None) -> bool:
    """Событие написал сам владелец алиасов (его аккаунт), а не кто-то, упомянувший имя."""
    if source in _SENDER_SOURCES and sender_id:
        return (source, f"user:{sender_id}") in aliases
    if source == "gmail" and sender:
        low = sender.lower()
        return any(src == "gmail" and ident.lower() in low for src, ident in aliases)
    return False


async def name_evidence(entity_id: int) -> list[dict[str, Any]]:
    """Текущие связи сущности, основанные на упоминании имени (не на её собственных сообщениях)."""
    async with get_session() as s:
        aliases = {(r[0], r[1]) for r in (await s.execute(
            text("SELECT source, identifier FROM entity_aliases WHERE entity_id = :e"),
            {"e": entity_id})).all()}
        rows = (await s.execute(text(
            "SELECT r.id, r.subject_entity_id AS s, r.predicate, r.object_entity_id AS o, r.fact, "
            "r.derived_from_event_id AS ev, e.source, e.occurred_at, "
            "e.metadata->>'sender_id' AS sender_id, e.metadata->>'from' AS sender "
            "FROM relationships r JOIN events e ON e.id = r.derived_from_event_id "
            "WHERE r.is_current AND (r.subject_entity_id = :e OR r.object_entity_id = :e) "
            "ORDER BY r.id"), {"e": entity_id})).mappings().all()
    mine = [r for r in rows
            if not authored_by(list(aliases), r["source"], r["sender_id"], r["sender"])]
    cards = await entity_cards([r["o"] if r["s"] == entity_id else r["s"] for r in mine])
    out = []
    for r in mine:
        other = r["o"] if r["s"] == entity_id else r["s"]
        out.append({"rel_id": r["id"], "subject_id": r["s"], "object_id": r["o"],
                    "predicate": r["predicate"], "fact": r["fact"], "other_id": other,
                    "other_name": cards[other].name if other in cards else f"№{other}",
                    "event_id": r["ev"], "source": r["source"],
                    "occurred_at": str(r["occurred_at"])})
    return out


async def _check(from_id: int, to_id: int, rel_ids: list[int]) -> None:
    if from_id == to_id:
        raise GraphEditError("source and target must differ")
    if not rel_ids or len(rel_ids) > MAX_MOVED:
        raise GraphEditError(f"rel_ids must hold 1..{MAX_MOVED} ids")
    allowed = {r["rel_id"] for r in await name_evidence(from_id)}
    foreign = sorted(set(rel_ids) - allowed)
    if foreign:
        raise GraphEditError(f"relationships {foreign} are not name-mention evidence of {from_id}")


async def _repoint_all(s: Any, from_id: int, to_id: int,
                       rel_ids: list[int]) -> list[tuple[str, int, dict, dict]]:
    await graph_edit.current_name(s, to_id)
    return [(outcome, rel_id, before, after) for rel_id in sorted(set(rel_ids))
            for outcome, before, after in [await graph_edit.repoint_relationship(s, rel_id, from_id, to_id)]]


async def preview_move(from_id: int, to_id: int, rel_ids: list[int]) -> list[dict[str, Any]]:
    """Что произойдёт с каждой связью, ничего не записывая: 'moved' или 'retired' (петля/дубль)."""
    await _check(from_id, to_id, rel_ids)
    async with get_session() as s:
        done = await _repoint_all(s, from_id, to_id, rel_ids)
        await s.rollback()
    return [{"rel_id": rel_id, "outcome": outcome} for outcome, rel_id, _, _ in done]


async def move_relationships(from_id: int, to_id: int, rel_ids: list[int], client: str) -> list[int]:
    """Переносит связи и пишет по строке журнала на каждую; возвращает id строк журнала."""
    await _check(from_id, to_id, rel_ids)
    audit_ids: list[int] = []
    async with get_session() as s:
        for outcome, rel_id, before, after in await _repoint_all(s, from_id, to_id, rel_ids):
            audit_ids.append(await audit.record(
                s, client=client, tool="relationship_move" if outcome == "moved" else "relationship_retire",
                args={"relationship_id": rel_id, "from_id": from_id, "to_id": to_id},
                kind="relationship", target_id=rel_id, before=before, after=after))
    return audit_ids

