"""Lightweight LLM relationship extractor — called by brain-triage.

For each event we ask the LLM: "are there any (subject, predicate, object)
tuples in this text where both endpoints are known entities?" Returns 0..N
relationships. Inserted into `relationships` with `derived_from_event_id`
so we can audit and roll back.

Why minimal: heavyweight graph extraction = OpenIE / RE models. Vera's
budget doesn't justify it; we want a small set of HIGH-CONFIDENCE links
(boss/coworker/spouse/founder-of) — not every passing mention.

Triage cost contract: ≤300 output tokens per event, capability='structured'.

The model contract (prompt, schema, parsing, outcome) lives in
`rel_extract_schema`, the author lookup in `rel_author`; both are re-exported
here. `extract_and_store` stays here with its collaborators imported at module
level, because tests patch `rel_extract.chat_async`, `rel_extract.judge_batch`
and the resolvers.
"""
from __future__ import annotations

import logging

from vera_shared.graph.rel_author import author_entity_of_event
from vera_shared.graph.rel_extract_schema import (
    PREDICATES,
    PROMPT,
    REL_EXTRACT_JSON_SCHEMA,
    SELF_TOKENS,
    RelExtractOutcome,
    parse_relationships,
)
from vera_shared.graph.rel_judge import Candidate, judge_batch
from vera_shared.graph.rel_text import End, Evidence, has_self_marker, single_token_name
from vera_shared.graph.rel_validate import (
    is_referential_name,
)
from vera_shared.graph.repo import (
    get_entity,
    resolve_entity_exact,
)
from vera_shared.graph.repo_relationships import (
    entity_names,
    resolve_strong_identifier,
    upsert_relationship,
)
from vera_shared.links.circle import EventCircle, event_circle, resolve_short_name
from vera_shared.llm.client import LLMCallFailed, chat_async

__all__ = [
    "PREDICATES",
    "PROMPT",
    "REL_EXTRACT_JSON_SCHEMA",
    "SELF_TOKENS",
    "RelExtractOutcome",
    "author_entity_of_event",
    "extract_and_store",
    "is_referential_name",
]

log = logging.getLogger(__name__)


async def extract_and_store(event_id: int, body: str) -> RelExtractOutcome:
    """LLM → проверка → запись. Решение, звать ли вообще, — `rel_policy`."""
    out = RelExtractOutcome()
    if not body or not body.strip():
        return out
    prompt = PROMPT.format(preds=", ".join(PREDICATES), body=body[:2000])
    try:
        raw, _meta = await chat_async(
            messages=[{"role": "user", "content": prompt}],
            capability="structured",
            response_format=REL_EXTRACT_JSON_SCHEMA,
            max_tokens=300,
            temperature=0.1,
            workflow="rel_extract",
        )
    except LLMCallFailed as e:
        out.llm_failed = True
        log.warning("rel_extract event=%s: LLM не ответила: %s", event_id, e)
        return out

    rels = parse_relationships(raw)
    if rels is None:
        out.llm_failed = True
        # Сам ответ в лог не пишем — в нём факты из личной переписки.
        log.warning("rel_extract event=%s: ответ не по схеме (%d символов)",
                    event_id, len(raw or ""))
        return out
    out.returned = len(rels)

    author_id: int | None | bool = False   # False = ещё не искали (lazy, 1 запрос)
    # Один и тот же человек обычно встречается в нескольких фактах подряд
    # («Игорь работает в X», «Игорь — начальник Y»). Каждый resolve — это своя
    # сессия к БД, поэтому в пределах одного события помним, что уже искали.
    resolved: dict[str, tuple[int | None, bool]] = {}
    described: dict[int, tuple[str, str]] = {}

    async def _resolve(name: str) -> tuple[int | None, bool, bool]:
        """(entity_id, найден_сильным_идентификатором, это_автор)."""
        nonlocal author_id
        low = name.lower().strip()
        if low in SELF_TOKENS:
            if author_id is False:
                author_id = await author_entity_of_event(event_id)
            return author_id or None, True, True
        if not is_referential_name(name):
            return None, False, False
        if low not in resolved:
            strong_id = await resolve_strong_identifier(name)
            if strong_id:
                resolved[low] = (strong_id, True)
            elif single_token_name(name):
                # Одно имя указывает на человека только внутри круга разговора: глобально
                # «Дима» — десятки людей, а единственный по точному имени — чужой тёзка.
                resolved[low] = (await _resolve_single(name), False)
            else:
                resolved[low] = (await resolve_entity_exact(name), False)
        entity_id, strong = resolved[low]
        return entity_id, strong, False

    circle: list[EventCircle | None] = []

    async def _resolve_in_circle(name: str) -> int | None:
        if not circle:
            circle.append(await event_circle(event_id))
        return await resolve_short_name(name, circle[0]) if circle[0] else None

    async def _resolve_single(name: str) -> int | None:
        """Организация из одного слова («Acme») — как раньше, по точному имени; человек —
        только по кругу разговора, даже если по имени он единственный."""
        by_name = await resolve_entity_exact(name)
        if by_name is not None and (await _describe(by_name))[1] != "person":
            return by_name
        return await _resolve_in_circle(name)

    async def _describe(entity_id: int) -> tuple[str, str]:
        if entity_id not in described:
            row = await get_entity(entity_id)
            described[entity_id] = (row.name, row.type) if row else ("", "")
        return described[entity_id]

    prepared: list[tuple[Candidate, str]] = []   # (кандидат, факт)
    for r in rels:
        subj = (r.get("subject") or "").strip()
        obj = (r.get("object") or "").strip()
        pred = (r.get("predicate") or "").strip().lower()
        if not subj or not obj or pred not in PREDICATES:
            out.rejected["malformed"] += 1
            continue
        subj_id, subj_strong, subj_author = await _resolve(subj)
        obj_id, obj_strong, obj_author = await _resolve(obj)
        if not subj_id or not obj_id:
            out.unresolved += 1
            continue
        # Проверяем сущности из графа, а не строки модели: «Я» резолвится в
        # автора, а тип конца (person/organization/…) есть только у сущности.
        subj_name, subj_type = await _describe(subj_id)
        obj_name, obj_type = await _describe(obj_id)
        fact = (r.get("fact") or "")[:500]
        if has_self_marker(fact):
            # «На Андрея (это я)»: конец с таким именем — автор. Если имя нашло ДРУГУЮ
            # сущность, валидация отклонит связь (`self_reference`), а не припишет её тёзке.
            if author_id is False:
                author_id = await author_entity_of_event(event_id)
            subj_author = subj_author or subj_id == author_id
            obj_author = obj_author or obj_id == author_id
            subj_strong, obj_strong = subj_strong or subj_author, obj_strong or obj_author
        names = await entity_names([subj_id, obj_id])
        evidence = Evidence(fact, End(tuple(names[subj_id]), subj_strong, subj_author),
                            End(tuple(names[obj_id]), obj_strong, obj_author))
        prepared.append((Candidate(((subj_id, subj_name, subj_type),
                                    (obj_id, obj_name, obj_type)), pred,
                                   float(r.get("confidence", 0.6)), evidence), fact))

    reasons = await judge_batch([c for c, _ in prepared], event_id=event_id, body=body)
    for (cand, fact), reason in zip(prepared, reasons, strict=True):
        if reason:
            out.rejected[reason] += 1
            continue
        if await upsert_relationship(
            subject_entity_id=cand.ends[0][0], object_entity_id=cand.ends[1][0],
            predicate=cand.predicate, fact=fact, confidence=cand.confidence,
            derived_from_event_id=event_id,
        ):
            out.inserted += 1

    log.info("rel_extract event=%s: вернула=%d не_найдено=%d отсеяно=%d %s записано=%d",
             event_id, out.returned, out.unresolved, sum(out.rejected.values()),
             dict(out.rejected), out.inserted)
    return out
