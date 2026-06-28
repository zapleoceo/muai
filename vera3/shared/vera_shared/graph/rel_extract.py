"""Lightweight LLM relationship extractor — called by brain-triage.

For each event we ask the LLM: "are there any (subject, predicate, object)
tuples in this text where both endpoints are known entities?" Returns 0..N
relationships. Inserted into `relationships` with `derived_from_event_id`
so we can audit and roll back.

Why minimal: heavyweight graph extraction = OpenIE / RE models. Vera's
budget doesn't justify it; we want a small set of HIGH-CONFIDENCE links
(boss/coworker/spouse/founder-of) — not every passing mention.

Triage cost contract: ≤300 output tokens per event, capability='structured'.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from vera_shared.db.engine import get_session
from vera_shared.llm.client import LLMCallFailed, chat

log = logging.getLogger(__name__)

PREDICATES = [
    "boss_of",          # X is boss of Y
    "reports_to",       # X reports to Y (inverse of boss_of, model picks one)
    "coworker_of",      # X and Y work together
    "co_founder_of",    # X is co-founder of org Y
    "works_at",         # X works at org Y
    "client_of",        # X is client of Y (org or person)
    "vendor_of",        # X provides services/goods to Y
    "spouse_of",        # symmetric
    "parent_of",        # X is parent of Y
    "child_of",         # inverse
    "friend_of",        # symmetric
    "lives_in",         # X lives in place Y
]

PROMPT = """Извлеки факты-связи между сущностями из текста сообщения.
Возвращай ТОЛЬКО JSON по схеме — без префиксов, без markdown.

Доступные предикаты: {preds}

Правила:
  - Только связи которые ЯВНО упомянуты в тексте, не выводи из контекста
  - Если subject/object — это «я» (Дима), используй "Дима"
  - Если нет уверенных связей — верни {{"relationships": []}}
  - Максимум 3 связи на одно сообщение

Schema:
{{
  "relationships": [
    {{"subject": "<name>", "predicate": "<one of above>",
      "object": "<name>", "fact": "<verbatim text justifying it>",
      "confidence": <0.0-1.0>}}
  ]
}}

Текст:
{body}"""


async def _resolve_entity(name: str) -> int | None:
    """Find entity_id by name or alias display_name match. None if unknown."""
    async with get_session() as s:
        # Exact name
        eid = (await s.execute(text(
            "SELECT id FROM entities WHERE LOWER(name) = LOWER(:n) LIMIT 1"
        ), {"n": name.strip()})).scalar()
        if eid:
            return eid
        # Alias display_name
        eid = (await s.execute(text(
            "SELECT entity_id FROM entity_aliases "
            "WHERE LOWER(display_name) = LOWER(:n) LIMIT 1"
        ), {"n": name.strip()})).scalar()
        return eid


async def extract_and_store(event_id: int, body: str) -> int:
    """Returns number of relationships inserted."""
    if not body or len(body) < 30:
        return 0
    prompt = PROMPT.format(preds=", ".join(PREDICATES), body=body[:2000])
    try:
        raw, _meta = await chat(
            messages=[{"role": "user", "content": prompt}],
            capability="structured",
            response_format={"type": "json_object"},
            max_tokens=300,
            temperature=0.1,
            workflow="rel_extract",
        )
    except LLMCallFailed as e:
        log.debug("rel_extract LLM fail event=%s: %s", event_id, e)
        return 0

    try:
        data = json.loads(raw)
        rels = data.get("relationships", [])
        if not isinstance(rels, list):
            return 0
    except json.JSONDecodeError:
        return 0

    inserted = 0
    for r in rels[:3]:
        if not isinstance(r, dict):
            continue
        subj = (r.get("subject") or "").strip()
        obj = (r.get("object") or "").strip()
        pred = (r.get("predicate") or "").strip().lower()
        if not subj or not obj or pred not in PREDICATES:
            continue
        if subj == obj:
            continue

        subj_id = await _resolve_entity(subj)
        obj_id = await _resolve_entity(obj)
        if not subj_id or not obj_id:
            log.debug("rel_extract: skip — entity not found (%s | %s)", subj, obj)
            continue

        conf = float(r.get("confidence", 0.6))
        fact = (r.get("fact") or "")[:500]

        async with get_session() as s:
            # Don't duplicate same (subj, pred, obj) — bump last_seen instead
            existing = (await s.execute(text(
                "SELECT id FROM relationships "
                "WHERE subject_entity_id=:s AND object_entity_id=:o "
                "AND predicate=:p LIMIT 1"
            ), {"s": subj_id, "o": obj_id, "p": pred})).scalar()
            if existing:
                await s.execute(text(
                    "UPDATE relationships SET last_seen_at = NOW(), "
                    "confidence = GREATEST(confidence, :c) WHERE id = :id"
                ), {"c": conf, "id": existing})
            else:
                await s.execute(text(
                    "INSERT INTO relationships "
                    "(subject_entity_id, object_entity_id, predicate, fact, "
                    " confidence, derived_from_event_id) "
                    "VALUES (:s, :o, :p, :f, :c, :ev)"
                ), {"s": subj_id, "o": obj_id, "p": pred,
                    "f": fact, "c": conf, "ev": event_id})
                inserted += 1

    if inserted:
        log.info("rel_extract event=%s inserted=%d", event_id, inserted)
    return inserted
