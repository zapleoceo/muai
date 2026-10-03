"""Кандидаты в прозвища: инициалы «Имя Отчество» из обращений к человеку и отчёт по области.

Предложение — не применение: строка `suggested` ждёт владельца (`decide_suggestion`).
Инициалы берутся из данных, не из справочника: если в переписке с человеком его
называют «Дмитрий Александрович» (имя в любой форме его имени + отчество), то «ДА» —
кандидат. Рядом считается, сколько раз токен встречается в областях (рабочие чаты /
вне их), чтобы владелец видел цену решения.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from sqlalchemy import select, text

from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.graph.dupe_keys import name_words, word_key
from vera_shared.ingest.envelope import message_body
from vera_shared.links.context import ContextBuilder, EventView, established_contacts
from vera_shared.links.name_forms import name_group
from vera_shared.links.nicknames import suggest_nickname
from vera_shared.links.scope import WORK, NicknameRule, in_scope, token_pattern

MIN_PATRONYMIC_USES = 2
SAMPLE_EVENTS = 600
SCAN_LIMIT = 20_000
_PAIR = re.compile(r"(?<!\w)([А-ЯЁІЇЄҐ][а-яёіїєґ'’-]+)\s+([А-ЯЁІЇЄҐ][а-яёіїєґ'’-]+"
                   r"(?:ович|евич|ьич|йович|овна|евна|ична|івна))(?!\w)")


def initials(first: str, patronymic: str) -> str:
    return (first[0] + patronymic[0]).upper()


def _first_forms(entity_name: str) -> tuple[set[str], set[int]]:
    words = name_words(entity_name)
    return set(words[:1]), {g for w in words if (g := name_group(w)) is not None}


def patronymic_forms(texts: list[str], entity_name: str) -> Counter[tuple[str, str]]:
    """Обращения «Имя Отчество» в текстах, где имя — форма имени этого человека."""
    keys, groups = _first_forms(entity_name)
    out: Counter[tuple[str, str]] = Counter()
    for body in texts:
        for first, patronymic in _PAIR.findall(body):
            key = word_key(first)
            if key in keys or name_group(key) in groups:
                out[(first, patronymic)] += 1
    return out


async def _dialogue_texts(entity_id: int) -> list[str]:
    """Тексты переписки с человеком: события, где он автор или получатель."""
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT e.content_text FROM event_entities l JOIN events e ON e.id = l.event_id "
            "WHERE l.entity_id = :i AND l.role IN ('author', 'recipient') "
            "ORDER BY e.occurred_at DESC LIMIT :n"), {"i": entity_id, "n": SAMPLE_EVENTS})).all()
    return [message_body(r[0]) for r in rows]


async def suggest_initials(entity_id: int, entity_name: str) -> list[str]:
    """Записать предложения-инициалы; → токены, которых раньше не знали."""
    forms = patronymic_forms(await _dialogue_texts(entity_id), entity_name)
    created: list[str] = []
    for (first, patronymic), uses in forms.most_common():
        if uses < MIN_PATRONYMIC_USES:
            continue
        token = initials(first, patronymic)
        reason = f"обращение «{first} {patronymic}» в переписке: {uses} раз"
        if await suggest_nickname(entity_id, token, reason, WORK):
            created.append(token)
    return created


async def scope_report(rule: NicknameRule, limit: int = SCAN_LIMIT) -> dict[str, Any]:
    """Сколько раз токен встречается в области и вне её (по сообщениям, не по связям):
    счёт для решения владельца, до применения. Чаты — по названию, топ-10 в каждой группе."""
    pattern = token_pattern(rule)
    column = EventRow.content_text
    like = column.like(f"%{rule.token}%") if rule.case_sensitive else column.ilike(f"%{rule.token}%")
    async with get_session() as s:
        rows = list((await s.execute(
            select(EventRow).where(like).order_by(EventRow.id.desc()).limit(limit))).scalars())
    hits = [r for r in rows if pattern.search(r.content_text or "")]
    builder = ContextBuilder()
    facts = await builder.build([EventView(r.id, r.source, r.content_text or "", r.metadata_ or {})
                                 for r in hits])
    strong = await established_contacts(rule.entity_id)
    inside: Counter[str] = Counter()
    outside: Counter[str] = Counter()
    for row in hits:
        title = str((row.metadata_ or {}).get("chat_title") or row.source)
        ok = in_scope(rule, facts[row.id].ctx, strong)
        (inside if ok else outside)[title] += 1
    return {"token": rule.token, "scanned": len(rows), "in_scope": sum(inside.values()),
            "out_of_scope": sum(outside.values()), "top_in": inside.most_common(10),
            "top_out": outside.most_common(10)}

