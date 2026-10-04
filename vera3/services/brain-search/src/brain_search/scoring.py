"""Ранжирование кандидатов.

Косинус считает Postgres по колонке halfvec и отдаёт колонкой `vec_sim`;
строка без него (события без вектора, режимы без вектора запроса) получает 0.
"""
from __future__ import annotations

from typing import Any

from brain_search.query_parse import BOT_AUTHOR_WEIGHT, source_weight
from brain_search.rows import Candidate
from brain_search.source_links import source_url


def row_similarity(row: Any, q_vec: list[float] | None) -> float:
    """Косинус из БД (`vec_sim`); у строки без него — 0."""
    if not q_vec:
        return 0.0
    vec_sim = Candidate.of(row).vec_sim
    return 0.0 if vec_sim is None else float(vec_sim)


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
        "source_url": source_url(c.source, c.source_event_id, c.source_permalink),
        "occurred_at": str(c.occurred_at),
        "content_preview": (c.content_text or "")[:400],
        "importance": c.importance,
    }) for score, c in score_candidates(rows, q_vec, acc_words)]
