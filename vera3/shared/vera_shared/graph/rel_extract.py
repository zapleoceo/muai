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
from email.utils import parseaddr

from sqlalchemy import text

from vera_shared.db.engine import get_session
from vera_shared.graph.repo import (
    find_entity_by_alias,
    resolve_entity_exact,
    upsert_relationship,
)
from vera_shared.llm.client import LLMCallFailed, chat_async

log = logging.getLogger(__name__)

# Первое лицо в тексте («я работаю в X») — это АВТОР сообщения, не сущность
# с именем «Я». Раньше «Я» резолвилось по имени и все такие связи падали на
# случайный аккаунт, чей first_name = «Я» (найдено вживую: 221 ребро от 6+
# разных авторов на одном чужом человеке).
SELF_TOKENS = {"я", "i", "me", "myself"}

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
  - Если subject/object — сам автор сообщения («я», «мне», от первого лица),
    пиши РОВНО "Я" — система сама подставит автора
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

# json_schema вместо json_object — форсит grammar-constrained decoding у
# провайдеров которые его поддерживают (gemini/openai/groq), так что
# битый JSON (частая причина потерь на cerebras gpt-oss) физически не
# может быть сгенерирован. predicate ограничен PREDICATES прямо в схеме —
# не полагаемся только на промпт-инструкцию.
REL_EXTRACT_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "rel_extract",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "relationships": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "subject": {"type": "string"},
                            "predicate": {"type": "string", "enum": PREDICATES},
                            "object": {"type": "string"},
                            "fact": {"type": "string"},
                            "confidence": {"type": "number", "minimum": 0.0,
                                           "maximum": 1.0},
                        },
                        "required": [
                            "subject", "predicate", "object", "fact", "confidence",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["relationships"],
            "additionalProperties": False,
        },
    },
}


async def author_entity_of_event(event_id: int) -> int | None:
    """Кто автор события — как entity_id. Это цель self-токенов («я»).

    telegram: sent → владелец (alias user:OWNER_TG_ID), received → отправитель
    (alias user:<sender_id>). gmail: sent → владелец, received → адрес From
    (alias gmail). Прочие источники (manual/claude/vera_chat) — владелец.
    None, если сущности-автора (ещё) нет в графе — тогда self-связь скипается,
    а не вешается на кого попало.
    """
    from vera_shared.projects.rules import OWNER_TG_ID
    async with get_session() as s:
        row = (await s.execute(text(
            "SELECT source, metadata FROM events WHERE id = :i"
        ), {"i": event_id})).first()
    if row is None:
        return None
    source, meta = row[0], row[1] or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    direction = meta.get("direction")

    if source == "telegram" and direction != "sent":
        sender = meta.get("sender_id")
        if sender is not None:
            return await find_entity_by_alias("telegram", f"user:{sender}")
        return None
    if source == "gmail" and direction != "sent":
        _, addr = parseaddr(meta.get("from", "") or "")
        if addr:
            return await find_entity_by_alias("gmail", addr.strip().lower())
        return None
    if source == "instagram" and direction != "sent":
        sender = meta.get("sender_id")
        if sender is not None:
            return await find_entity_by_alias("instagram", f"user:{sender}")
        return None
    # sent-события любого источника и «свои» источники — владелец
    return await find_entity_by_alias("telegram", f"user:{OWNER_TG_ID}")


async def extract_and_store(event_id: int, body: str) -> int:
    """Returns number of relationships inserted."""
    if not body or len(body) < 30:
        return 0
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
    author_id: int | None | bool = False   # False = ещё не искали (lazy, 1 запрос)

    async def _resolve(name: str) -> int | None:
        nonlocal author_id
        if name.lower() in SELF_TOKENS:
            if author_id is False:
                author_id = await author_entity_of_event(event_id)
            return author_id
        return await resolve_entity_exact(name)

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

        subj_id = await _resolve(subj)
        obj_id = await _resolve(obj)
        if not subj_id or not obj_id or subj_id == obj_id:
            log.debug("rel_extract: skip — entity not found (%s | %s)", subj, obj)
            continue

        conf = float(r.get("confidence", 0.6))
        fact = (r.get("fact") or "")[:500]

        # Canonical upsert lives in repo.py — single source of truth for the
        # (subject, predicate, object) soft-upsert (was duplicated raw SQL
        # here that, unlike repo, never back-filled a missing `fact`).
        if await upsert_relationship(
            subject_entity_id=subj_id, object_entity_id=obj_id,
            predicate=pred, fact=fact, confidence=conf,
            derived_from_event_id=event_id,
        ):
            inserted += 1

    if inserted:
        log.info("rel_extract event=%s inserted=%d", event_id, inserted)
    return inserted
