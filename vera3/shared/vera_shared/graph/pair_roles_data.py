"""Чтение улик пары из `event_entities`: личка и письма, обращения друг к другу, упоминания
третьими лицами. Только SELECT'ы; отбор и обрезка — `pair_roles_pack`.

Каждый запрос ограничен `FETCH_LIMIT` новейших строк: нужна история, а не вся таблица, и
выборка по эпохам (`sample_over_time`) всё равно берёт срез. Авторы помечены A (меньший id
пары), B (больший) или X (третье лицо).
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from sqlalchemy import text

from vera_shared.db.engine import get_session
from vera_shared.events.visibility import not_hidden_sql
from vera_shared.graph.connection_data import claims_within, shared_work, work_idents
from vera_shared.graph.pair_roles_types import (
    KIND_CHAT,
    KIND_DM,
    KIND_MAIL,
    KIND_MENTION,
    PackMessage,
    PairSide,
)
from vera_shared.graph.pair_stats import PairStats
from vera_shared.graph.rel_canon import SYMMETRIC, canonical_edge
from vera_shared.ingest.envelope import message_body
from vera_shared.links.nicknames import active_rules
from vera_shared.links.read import json_dict

FETCH_LIMIT = 3000
MIN_MENTION_CONFIDENCE = 0.6
_COLUMNS = "e.id, e.source, e.occurred_at, e.content_text, e.metadata, e.project"
_VISIBLE = not_hidden_sql("e")
_TAIL = f" AND {_VISIBLE} ORDER BY e.occurred_at DESC LIMIT :n"
_MENTION_OK = "AND {t}.scope_ok AND {t}.confidence >= :minc"

# Все запросы идут ОТ ЯКОРЯ — менее «густого» конца пары (для пары владельца это собеседник): по
# индексу (entity_id, role, event_id) берутся только события якоря (сотни-тысячи), а второй конец
# проверяется присоединением по event_id. Старт от владельца читал бы сотни тысяч строк.
# Личка и письма: «кто-то» пишет, «кто-то» — адресат. `{author}` — столбец автора.
_DIRECT = (f"SELECT {_COLUMNS}, {{author}} AS author_id FROM event_entities la "
           "JOIN event_entities lb ON lb.event_id = la.event_id AND lb.entity_id = {other} "
           "AND lb.role = '{rb}' JOIN events e ON e.id = la.event_id "
           f"WHERE la.entity_id = :anchor AND la.role = '{{ra}}'{_TAIL}")
# Один из пары пишет в чате и называет другого (имя, @ник, прозвище в области).
_ADDRESS = (f"SELECT {_COLUMNS}, {{author}} AS author_id FROM event_entities la "
            "JOIN event_entities lb ON lb.event_id = la.event_id AND lb.entity_id = {other} "
            "AND lb.role = '{rb}' {mention} JOIN events e ON e.id = la.event_id "
            f"WHERE la.entity_id = :anchor AND la.role = '{{ra}}' AND e.source <> 'gmail' "
            f"{{mention_a}}{_TAIL}")
# Третьи лица говорят о ком-то из пары («ДА просил ознакомиться…»): якорь `:about`.
_THIRD = (f"SELECT {_COLUMNS}, lm.entity_id AS about_id, "
          "(SELECT la.entity_id FROM event_entities la WHERE la.event_id = e.id "
          " AND la.role = 'author' LIMIT 1) AS author_id "
          "FROM event_entities lm JOIN events e ON e.id = lm.event_id "
          "WHERE lm.entity_id = :about AND lm.role = 'mentioned' AND lm.scope_ok "
          "AND lm.confidence >= :minc "
          "AND NOT EXISTS (SELECT 1 FROM event_entities lx WHERE lx.event_id = e.id "
          " AND lx.role = 'author' AND lx.entity_id IN (:anchor, :other))"
          f"{_TAIL}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _label(entity_id: int | None, a: int, b: int) -> str:
    return "A" if entity_id == a else "B" if entity_id == b else "X"


def _day(value: Any) -> str:
    return value.date().isoformat() if hasattr(value, "date") else (
        m.group(0) if (m := _DATE.match(str(value))) else str(value))


def _body(row: Any, meta: dict[str, Any]) -> str:
    body = " ".join(message_body(row["content_text"]).split())
    if row["source"] != "gmail":
        return body
    return (f"[от {meta.get('from', '')} кому {meta.get('to', '')}] тема: "
            f"{meta.get('subject', '')} | {body}")


def _message(row: Any, kind: str, a: int, b: int, about: str = "") -> PackMessage:
    meta = json_dict(row["metadata"]) or {}
    channel = "mail" if row["source"] == "gmail" else str(
        meta.get("chat_title") or ("dm" if kind == KIND_DM else row["source"]))
    return PackMessage(f"m{row['id']}", _day(row["occurred_at"]), kind,
                       _label(row["author_id"], a, b), _body(row, meta), channel, about)


async def _run(sql: str, **params: int) -> list[Any]:
    async with get_session() as s:
        return list((await s.execute(text(sql), {"n": FETCH_LIMIT, "minc": MIN_MENTION_CONFIDENCE,
                                                 **params})).mappings())


def _direct_sql() -> list[tuple[str, str]]:
    """(SQL, вид): якорь — автор, другой — адресат; и наоборот."""
    return [(_DIRECT.format(author="la.entity_id", other=":other", ra="author", rb="recipient"), "direct"),
            (_DIRECT.format(author="lb.entity_id", other=":other", ra="recipient", rb="author"), "direct")]


def _address_sql() -> list[tuple[str, str]]:
    """(SQL, вид): якорь пишет и называет другого; якоря называет другой."""
    return [(_ADDRESS.format(author="la.entity_id", other=":other", ra="author", rb="mentioned",
                             mention=_MENTION_OK.format(t="lb"), mention_a=""), "address"),
            (_ADDRESS.format(author="lb.entity_id", other=":other", ra="mentioned", rb="author",
                             mention="", mention_a=_MENTION_OK.format(t="la")), "address")]


async def pair_messages(a: int, b: int, owner: int | None = None
                        ) -> tuple[list[PackMessage], Counter[str]]:
    """Все виды улик пары и счёт проектов событий (по колонке `events.project`). Якорь —
    конец пары не владелец (у владельца события — почти весь мозг); упоминания третьими лицами
    берутся о якоре, а о втором конце — только если владельца в паре нет."""
    anchor, other = (b, a) if owner == a else (a, b)
    third = [anchor] + ([other] if owner not in (a, b) else [])
    plan = [(sql, kind, {"anchor": anchor, "other": other}) for sql, kind in _direct_sql()]
    plan += [(sql, KIND_CHAT, {"anchor": anchor, "other": other}) for sql, _ in _address_sql()]
    plan += [(_THIRD, KIND_MENTION, {"about": t, "anchor": anchor, "other": other}) for t in third]
    messages: list[PackMessage] = []
    projects: Counter[str] = Counter()
    seen: set[int] = set()       # личное сообщение с обращением по имени — одно, а не «dm» и «chat»
    for sql, kind, params in plan:
        for row in await _run(sql, **params):
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            this = kind if kind in (KIND_CHAT, KIND_MENTION) else (
                KIND_MAIL if row["source"] == "gmail" else KIND_DM)
            about = _label(row["about_id"], a, b) if kind == KIND_MENTION else ""
            messages.append(_message(row, this, a, b, about))
            if row["project"]:
                projects[row["project"]] += 1
    return messages, projects


async def pair_sides(a: int, b: int, owner: int | None) -> tuple[PairSide, PairSide]:
    async with get_session() as s:
        names = dict((await s.execute(
            text("SELECT id, name FROM entities WHERE id IN (:a, :b)"), {"a": a, "b": b})).all())
        aliases = (await s.execute(
            text("SELECT entity_id, identifier FROM entity_aliases WHERE source = 'gmail' "
                 "AND entity_id IN (:a, :b)"), {"a": a, "b": b})).all()
    rules = await active_rules()

    def side(label: str, eid: int) -> PairSide:
        return PairSide(label, eid, names.get(eid, ""), eid == owner,
                        tuple(sorted(i for e, i in aliases if e == eid)),
                        tuple(sorted(r.token for r in rules if r.entity_id == eid)))

    return side("A", a), side("B", b)


async def asserted_roles(a: int, b: int) -> list[dict[str, Any]]:
    """Записанные роли пары (с числом записей и пометкой «задана руками»): модель видит,
    что граф уже знает, и не обязана это «открывать» заново."""
    grouped: dict[tuple[str, int | None], list[Any]] = {}
    for claim in await claims_within([a, b]):
        s, p, _ = canonical_edge(claim.subject_id, claim.predicate, claim.object_id)
        grouped.setdefault((p, None if p in SYMMETRIC else s), []).append(claim)
    return [{"predicate": p, "subject": "both" if s is None else _label(s, a, b),
             "records": len(claims), "manual": any(c.manual for c in claims)}
            for (p, s), claims in sorted(grouped.items(), key=lambda kv: kv[0][0])]


async def work_context(a: int, b: int) -> bool:
    idents = await work_idents([a, b])
    return shared_work(idents.get(a), idents.get(b))


def stats_signals(stats: PairStats) -> dict[str, Any]:
    return {k: v for k, v in stats.as_dict().items() if v}
