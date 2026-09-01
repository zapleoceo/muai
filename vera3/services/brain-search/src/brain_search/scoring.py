"""Ранжирование кандидатов.

Косинус считается циклом на Python, потому что эмбеддинги лежат в JSONB, а
не в колонке `vector` — расширение pgvector есть в образе, но не включено
(см. docs/brain.md). Когда включится, `_cosine` уйдёт целиком: ближайшие
будут отбираться индексом, а не перебором двухсот строк на запрос.
"""
from __future__ import annotations

from typing import Any

from brain_search.query_parse import source_weight


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


def score_rows(rows, q_vec: list[float] | None,
               acc_words: list[str]) -> list[tuple[float, dict[str, Any]]]:
    """(score, превью) по убыванию. Слагаемые намеренно разной величины:
    ts_rank×2 — прямое текстовое совпадение, косинус — смысловая близость,
    importance/200 — лёгкий наклон в сторону важного, +1 за совпадение по
    account (иначе англоязычное письмо проекта с rank=0 не поднимется)."""
    out: list[tuple[float, dict[str, Any]]] = []
    for r in rows:
        ts_rank = float(r[7]) if r[7] is not None else 0.0
        score = ts_rank * 2.0
        emb = r[6]
        if q_vec and emb:
            score += cosine(q_vec, emb)
        if r[5]:
            score += r[5] / 200.0
        account_l = (r[8] or "").lower() if len(r) > 8 else ""
        if account_l and any(w in account_l for w in acc_words):
            score += 1.0
        score *= source_weight(r[1])
        out.append((score, {
            "event_id": r[0],
            "source": r[1],
            "occurred_at": str(r[3]),
            "content_preview": (r[4] or "")[:400],
            "importance": r[5],
        }))
    out.sort(key=lambda x: x[0], reverse=True)
    return out
