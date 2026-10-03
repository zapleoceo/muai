"""Проход `verify` чистки: связи с одним словом вместо имени судит модель.

Связи пары с устоявшимся общением (`connections.established_pairs`: сотни личных
сообщений, общие рабочие чаты) модель не судит: роль таких людей определяет
модель связи по числу и виду контактов, а не одна фраза. Они остаются в плане
действием `skip` с правилом `pair_established` и не стоят вызовов брокера.

План строится из вердиктов `rel_verify`: «no» — `retire` («unclear» — только у иерархии), «yes» —
связь остаётся (в плане записана как `skip` с цитатой для аудита), «error»
(сбой брокера) — не трогается и считается в `unverified`: повторите прогон,
уже вынесенные вердикты берутся из кэша-файла. Кэш — JSON Lines, дописывается
по мере готовности, поэтому прерванный прогон продолжается с места остановки.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.events.visibility import HIDDEN_STATUS
from vera_shared.graph.pair_stats import ordered
from vera_shared.graph.rel_cleanup import Action, Row, retire_action
from vera_shared.graph.rel_verify import (
    ERROR,
    NO,
    UNCLEAR,
    YES,
    Cache,
    EdgeQuery,
    Verdict,
    verify_many,
)
from vera_shared.ingest.envelope import message_body

RULE_WEAK = "weak_name"
RULE_VERIFIED = "weak_name_verified"
RULE_ESTABLISHED = "pair_established"


def load_cache(path: Path) -> Cache:
    cache: Cache = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            cache[item["key"]] = Verdict(item["verdict"], item["quote"], item["cost_usd"])
    return cache


def _append(path: Path, query: EdgeQuery, verdict: Verdict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"key": query.key, "verdict": verdict.verdict,
                             "quote": verdict.quote, "cost_usd": verdict.cost_usd},
                            ensure_ascii=False) + "\n")


async def event_texts(event_ids: list[int]) -> dict[int, str]:
    """Тексты видимых событий; скрытого или пропавшего в словаре нет."""
    async with get_session() as s:
        rows = (await s.execute(select(EventRow.id, EventRow.content_text)
                                .where(EventRow.id.in_(event_ids),
                                       EventRow.triage_status != HIDDEN_STATUS))).all()
    return {eid: message_body(text) for eid, text in rows}

#: Предикаты, где направление и есть смысл связи.
HIERARCHY_PREDICATES = frozenset({"boss_of", "reports_to"})


def _action(row: Row, verdict: Verdict) -> Action:
    # «unclear» связь не гасит: среди связей с одиночным именем есть правда
    # («Маша — дочь»), и сомнение модели — не повод её терять (решение 04.10.2026).
    # Кроме иерархии: там «unclear» значит «не понятно, кто чей начальник», а
    # связь с неверным направлением хуже отсутствующей (аудит 04.10: #130).
    if verdict.verdict == NO or (verdict.verdict == UNCLEAR
                                 and row["predicate"] in HIERARCHY_PREDICATES):
        return {**retire_action(row, RULE_WEAK), "verdict": verdict.verdict}
    return {"action": "skip", "rule": RULE_VERIFIED, "rel_id": row["id"], "keep_id": None,
            "brief": f"{row['subject_name']} -[{row['predicate']}]-> {row['object_name']}",
            "verdict": verdict.verdict, "quote": verdict.quote}


def _pair(row: Row) -> tuple[int, int]:
    return ordered(row["subject_entity_id"], row["object_entity_id"])


def _established_skip(row: Row) -> Action:
    return {"action": "skip", "rule": RULE_ESTABLISHED, "rel_id": row["id"], "keep_id": None,
            "brief": f"{row['subject_name']} -[{row['predicate']}]-> {row['object_name']}"}


async def verify_plan(candidates: list[Row], cache_path: Path, *, limit: int | None = None,
                      concurrency: int = 4,
                      established: set[tuple[int, int]] | None = None,
                      ) -> tuple[list[Action], dict[str, Any]]:
    """(действия, статистика). `limit` — пробная партия: первые N кандидатов, которых
    не освободило общение пары (`established` — упорядоченные пары)."""
    exempt = [r for r in candidates if _pair(r) in (established or ())]
    candidates = [r for r in candidates if _pair(r) not in (established or ())]
    batch = candidates[:limit] if limit else candidates
    texts = await event_texts(sorted({r["derived_from_event_id"] for r in batch}))
    cache = load_cache(cache_path)

    async def remember(query: EdgeQuery, verdict: Verdict) -> None:
        if verdict.verdict != ERROR:
            _append(cache_path, query, verdict)

    queries = [EdgeQuery(r["derived_from_event_id"], r["subject_name"], r["predicate"],
                         r["object_name"]) for r in batch]
    fresh = [q for q in queries if q.key not in cache]
    await verify_many(((q, texts.get(q.event_id, "")) for q in fresh), cache=cache,
                      concurrency=concurrency, on_done=remember)
    actions: list[Action] = [_established_skip(r) for r in exempt]
    stats: dict[str, Any] = {"candidates": len(candidates) + len(exempt),
                             "established": len(exempt), "checked": len(batch),
                             "from_cache": len(queries) - len(fresh), YES: 0, NO: 0,
                             UNCLEAR: 0, "unverified": 0, "cost_usd": 0.0}
    for row, query in zip(batch, queries, strict=True):
        verdict = cache.get(query.key)
        if verdict is None:
            stats["unverified"] += 1
            continue
        stats[verdict.verdict] += 1
        stats["cost_usd"] += verdict.cost_usd
        actions.append(_action(row, verdict))
    for number, action in enumerate(actions, 1):
        action["id"] = number
    return actions, stats
