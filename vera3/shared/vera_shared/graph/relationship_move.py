"""«Связи не про этого человека»: перенос связей, которые держатся на упоминании имени.

Случай: чужой Telegram-аккаунт «Діма» получил связи, потому что извлекатель связал имя
из чужих сообщений с карточкой по совпадению имени. Объединять карточки нельзя (аккаунт
чужой), а связи на самом деле про другого человека. Переносимыми считаются связи, чьё
событие-источник ТОЧНО написал кто-то другой (у события есть автор, и он не этот человек).
Если автора определить нельзя (нет `sender_id`, нет адреса отправителя, другой источник) —
авторство НЕИЗВЕСТНО, такую связь не трогаем: перенести чужую правду хуже, чем оставить на месте.
Ручные связи (без события) и связи по его собственным сообщениям тоже не переносятся. Для
владельца «его сообщение» — в ЛЮБОМ источнике: telegram-id, адреса gmail из алиасов и атрибута email.

Перенос меняет концы записи (`graph.edit.repoint_relationship`), каждая правка — строка
`mcp_audit` (`relationship_move`; петля или дубль — `relationship_retire`; уже существовавшая,
но погашенная цель возвращается — `relationship_revive`), откат общий.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from vera_shared.db.engine import get_session
from vera_shared.graph import edit as graph_edit
from vera_shared.graph.connection_data import entity_cards
from vera_shared.graph.edit import GraphEditError
from vera_shared.journal import audit
from vera_shared.projects.rules import OWNER_TG_ID

MAX_MOVED = 200
_SENDER_SOURCES = ("telegram", "slack", "instagram")


def authorship(aliases: list[tuple[str, str]], source: str, sender_id: str | None,
               sender: str | None, is_owner: bool = False) -> bool | None:
    """True — событие написал владелец алиасов; False — точно другой; None — неизвестно."""
    if is_owner and sender_id and str(sender_id) == str(OWNER_TG_ID):
        return True
    if source in _SENDER_SOURCES:
        if not sender_id:
            return None
        return (source, f"user:{sender_id}") in aliases
    if source == "gmail":
        if not sender:
            return None
        low = sender.lower()
        return any(src == "gmail" and ident.lower() in low for src, ident in aliases)
    return None


def authored_by(aliases: list[tuple[str, str]], source: str, sender_id: str | None,
                sender: str | None) -> bool:
    """Совместимость: True только при доказанном авторстве."""
    return authorship(aliases, source, sender_id, sender) is True


def _attrs(raw: Any) -> dict[str, Any]:
    return json.loads(raw or "{}") if isinstance(raw, str) else dict(raw or {})


async def _identity(entity_id: int) -> tuple[list[tuple[str, str]], bool]:
    """Алиасы человека (с адресом из атрибутов) и признак «это владелец»."""
    async with get_session() as s:
        aliases = [(r[0], r[1]) for r in (await s.execute(
            text("SELECT source, identifier FROM entity_aliases WHERE entity_id = :e"),
            {"e": entity_id})).all()]
        raw = (await s.execute(text("SELECT attributes FROM entities WHERE id = :e"),
                               {"e": entity_id})).scalar_one_or_none()
    email = _attrs(raw).get("email")
    if email:
        aliases.append(("gmail", str(email)))
    return aliases, ("telegram", f"user:{OWNER_TG_ID}") in aliases


async def name_evidence(entity_id: int) -> list[dict[str, Any]]:
    """Текущие связи сущности, основанные на упоминании имени (автор события точно другой)."""
    aliases, is_owner = await _identity(entity_id)
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT r.id, r.subject_entity_id AS s, r.predicate, r.object_entity_id AS o, r.fact, "
            "r.derived_from_event_id AS ev, e.source, e.occurred_at, "
            "e.metadata->>'sender_id' AS sender_id, e.metadata->>'from' AS sender "
            "FROM relationships r JOIN events e ON e.id = r.derived_from_event_id "
            "WHERE r.is_current AND (r.subject_entity_id = :e OR r.object_entity_id = :e) "
            "ORDER BY r.id"), {"e": entity_id})).mappings().all()
    mine = [r for r in rows
            if authorship(aliases, r["source"], r["sender_id"], r["sender"], is_owner) is False]
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


async def _repoint_all(s: Any, from_id: int, to_id: int, rel_ids: list[int]) -> list[dict[str, Any]]:
    await graph_edit.current_name(s, to_id)
    done = []
    for rel_id in sorted(set(rel_ids)):
        outcome, before, after, twin = await graph_edit.repoint_relationship(s, rel_id, from_id, to_id)
        done.append({"rel_id": rel_id, "outcome": outcome, "before": before, "after": after, "twin": twin})
    return done


async def preview_move(from_id: int, to_id: int, rel_ids: list[int]) -> list[dict[str, Any]]:
    """Что произойдёт с каждой связью, ничего не записывая: 'moved', 'retired' (петля/дубль)
    или 'revived' (цель была погашена — возвращается)."""
    await _check(from_id, to_id, rel_ids)
    async with get_session() as s:
        done = await _repoint_all(s, from_id, to_id, rel_ids)
        await s.rollback()
    return [{"rel_id": d["rel_id"], "outcome": d["outcome"]} for d in done]


async def move_relationships(from_id: int, to_id: int, rel_ids: list[int], client: str) -> list[int]:
    """Переносит связи и пишет по строке журнала на каждое изменение; возвращает id строк журнала."""
    await _check(from_id, to_id, rel_ids)
    audit_ids: list[int] = []
    args = {"from_id": from_id, "to_id": to_id}
    async with get_session() as s:
        for d in await _repoint_all(s, from_id, to_id, rel_ids):
            tool = "relationship_move" if d["outcome"] == "moved" else "relationship_retire"
            if d["twin"] is not None:
                twin_id, twin_before, twin_after = d["twin"]
                audit_ids.append(await audit.record(
                    s, client=client, tool="relationship_revive", args={**args, "relationship_id": twin_id},
                    kind="relationship", target_id=twin_id, before=twin_before, after=twin_after))
            audit_ids.append(await audit.record(
                s, client=client, tool=tool, args={**args, "relationship_id": d["rel_id"]},
                kind="relationship", target_id=d["rel_id"], before=d["before"], after=d["after"]))
    return audit_ids
