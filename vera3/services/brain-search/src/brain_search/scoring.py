"""Ранжирование кандидатов.

Косинус приходит двумя путями. Когда эмбеддинги в колонке halfvec
(миграция 030), его считает Postgres и отдаёт колонкой `vec_sim` — JSONB не
разбирается. Строке, до которой бэкфил ещё не дошёл, и всем строкам на базе
без колонки (SQLite в тестах, прод до наката) косинус считается на Python
из JSONB — штатная ветка, а не заглушка.
"""
from __future__ import annotations

import json
from typing import Any

from brain_search.query_parse import BOT_AUTHOR_WEIGHT, source_weight
from brain_search.rows import Candidate


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    # strict=True безопасен: разная длина отсеяна строкой выше
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def row_similarity(row: Any, q_vec: list[float] | None) -> float:
    """Сходство из БД, если оно есть, иначе косинус по JSONB."""
    if not q_vec:
        return 0.0
    cand = Candidate.of(row)
    if cand.vec_sim is not None:
        return float(cand.vec_sim)
    emb = cand.embedding
    # asyncpg-диалект SQLAlchemy разбирает JSONB в list сам, SQLite отдаёт
    # текстом — без разбора косинус молча выходил бы 0 на разнице длин
    if isinstance(emb, str):
        emb = json.loads(emb)
    return cosine(q_vec, emb) if emb else 0.0


def score_candidates(rows, q_vec: list[float] | None,
                     acc_words: list[str]) -> list[tuple[float, Candidate]]:
    """(score, строка) по убыванию. Слагаемые намеренно разной величины:
    ts_rank×2 — прямое текстовое совпадение, косинус — смысловая близость,
    importance/200 — лёгкий наклон в сторону важного, +1 за совпадение по
    account (иначе англоязычное письмо проекта с rank=0 не поднимется).
    Множитель — вес источника и вес автора-бота (query_parse)."""
    out: list[tuple[float, Candidate]] = []
    for row in rows:
        c = Candidate.of(row)
        score = float(c.rank or 0.0) * 2.0 + row_similarity(c, q_vec)
        if c.importance:
            score += c.importance / 200.0
        account_l = (c.account or "").lower()
        if account_l and any(w in account_l for w in acc_words):
            score += 1.0
        score *= source_weight(c.source) * (BOT_AUTHOR_WEIGHT if c.is_bot else 1.0)
        out.append((score, c))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def score_rows(rows, q_vec: list[float] | None,
               acc_words: list[str]) -> list[tuple[float, dict[str, Any]]]:
    """(score, превью) для ответа /search."""
    return [(score, {
        "event_id": c.id,
        "source": c.source,
        "occurred_at": str(c.occurred_at),
        "content_preview": (c.content_text or "")[:400],
        "importance": c.importance,
    }) for score, c in score_candidates(rows, q_vec, acc_words)]
