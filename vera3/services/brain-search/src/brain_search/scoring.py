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


def row_similarity(row: Any, q_vec: list[float] | None) -> float:
    """Сходство из БД, если оно есть, иначе косинус по JSONB."""
    if not q_vec:
        return 0.0
    db_sim = getattr(row, "vec_sim", None)
    if db_sim is not None:
        return float(db_sim)
    emb = row[6]
    # asyncpg-диалект SQLAlchemy разбирает JSONB в list сам, SQLite отдаёт
    # текстом — без разбора косинус молча выходил бы 0 на разнице длин
    if isinstance(emb, str):
        emb = json.loads(emb)
    return cosine(q_vec, emb) if emb else 0.0


def score_rows(rows, q_vec: list[float] | None,
               acc_words: list[str]) -> list[tuple[float, dict[str, Any]]]:
    """(score, превью) по убыванию. Слагаемые намеренно разной величины:
    ts_rank×2 — прямое текстовое совпадение, косинус — смысловая близость,
    importance/200 — лёгкий наклон в сторону важного, +1 за совпадение по
    account (иначе англоязычное письмо проекта с rank=0 не поднимется)."""
    out: list[tuple[float, dict[str, Any]]] = []
    for r in rows:
        ts_rank = float(r[7]) if r[7] is not None else 0.0
        score = ts_rank * 2.0 + row_similarity(r, q_vec)
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
