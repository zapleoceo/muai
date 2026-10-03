"""Пакет улик пары: отбор сообщений по эпохам, обрезка, хэш — чистые функции.

Не «последние N сообщений» (там — сегодняшняя рутина), а срез всей истории: сообщения
делятся на равные по числу отрезки времени, из каждого берутся самые информативные
(длина в разумных пределах + слова-маркеры из лексикона). Лимиты — по числу
сообщений каждого вида и по общему объёму текста (≈ токены ÷ 3,5).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from vera_shared.graph.pair_roles_lexicon import CUE
from vera_shared.graph.pair_roles_types import (
    KIND_CHAT,
    KIND_DM,
    KIND_MAIL,
    KIND_MENTION,
    Evidence,
    PackMessage,
    PairSide,
)

PROMPT_VERSION = "2"
MAX_PACK_CHARS = 24_000
QUOTA = {KIND_DM: 40, KIND_CHAT: 15, KIND_MENTION: 30, KIND_MAIL: 15}
MAX_CHARS = {KIND_DM: 500, KIND_CHAT: 500, KIND_MENTION: 450, KIND_MAIL: 900}
MAX_BUCKETS = 12
HEAD_SHARE = 0.7          # у длинного текста сохраняем голову и хвост: подпись — в хвосте
IDEAL_LEN = 240


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = int(limit * HEAD_SHARE)
    return f"{text[:head]} … {text[-(limit - head):]}"


def informativeness(text: str) -> float:
    """Чем ближе длина к IDEAL_LEN и больше маркеров, тем выше; пустые реплики — ноль."""
    if len(text.strip()) < 12:
        return 0.0
    length = min(len(text), IDEAL_LEN * 2) / (IDEAL_LEN * 2)
    return length + 0.6 * min(len(CUE.findall(text)), 3)


def sample_over_time(messages: list[PackMessage], limit: int) -> list[PackMessage]:
    """До `limit` сообщений, равномерно по времени; в каждом отрезке — самые информативные.
    Результат в хронологическом порядке, детерминирован."""
    ordered = sorted(messages, key=lambda m: (m.at, m.id))
    if len(ordered) <= limit:
        return ordered
    buckets = min(limit, MAX_BUCKETS)
    size = len(ordered) / buckets
    chosen: list[PackMessage] = []
    for i in range(buckets):
        part = ordered[int(i * size):int((i + 1) * size)]
        share = limit // buckets + (1 if i >= buckets - limit % buckets else 0)
        top = sorted(part, key=lambda m: (-m.score, m.at, m.id))[:share]
        chosen.extend(top)
    return sorted(chosen, key=lambda m: (m.at, m.id))


def _trim_to_budget(messages: list[PackMessage], budget: int) -> list[PackMessage]:
    """Лишнее убирается с наименее информативных, порядок времени сохраняется."""
    kept = list(messages)
    total = sum(len(m.text) + 40 for m in kept)
    for victim in sorted(messages, key=lambda m: (m.score, m.at)):
        if total <= budget:
            break
        kept.remove(victim)
        total -= len(victim.text) + 40
    return kept


def select_messages(raw: list[PackMessage], budget: int = MAX_PACK_CHARS) -> list[PackMessage]:
    scored = [replace(m, text=clip(m.text, MAX_CHARS.get(m.kind, 500)),
                      score=informativeness(m.text)) for m in raw if m.text.strip()]
    picked: list[PackMessage] = []
    for kind, quota in QUOTA.items():
        picked.extend(sample_over_time([m for m in scored if m.kind == kind], quota))
    return _trim_to_budget(sorted(picked, key=lambda m: (m.at, m.id)), budget)


def digest_of(a: PairSide, b: PairSide, signals: dict[str, Any],
              messages: list[PackMessage]) -> str:
    """Хэш того, что видит модель: версия промпта, концы, прозвища, адреса, id сообщений."""
    payload = {"v": PROMPT_VERSION, "a": [a.entity_id, a.addresses, a.nicknames],
               "b": [b.entity_id, b.addresses, b.nicknames],
               "asserted": signals.get("asserted_in_graph"), "ids": [m.id for m in messages]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str,
                                     ensure_ascii=False).encode()).hexdigest()


def build_evidence(a: PairSide, b: PairSide, signals: dict[str, Any],
                   raw: list[PackMessage]) -> Evidence:
    messages = select_messages(raw)
    corpus_parts = [m.text for m in messages]
    corpus_parts += [a.name, b.name, *a.addresses, *b.addresses, *a.nicknames, *b.nicknames]
    corpus_parts += [s for ex in signals.get("title_mentions", {}).values() for s in ex]
    return Evidence(a, b, signals, tuple(messages), digest_of(a, b, signals, messages),
                    "\n".join(corpus_parts))
