"""Смысловые кандидаты: ближайшие к вопросу по вектору во ВСЁМ корпусе.

До этого модуля вектор запроса только переупорядочивал ≤200 строк, уже
отобранных полнотекстом `to_tsvector('russian')`, временем или проектом.
Замер на проде 2026-09-13: 444 тыс. эмбеддингов, и текст, не делящий с
вопросом ни одного слова («сколько стоит аренда» → письмо «invoice for the
villa»), не находился никогда — до косинуса он просто не доезжал.

Здесь кандидаты берутся из ANN-индекса (`vectors.ann_candidates_sql`) с теми
же фильтрами проекта/времени, что у основной выборки, и ОБЪЕДИНЯЮТСЯ с ней:
режимы fts/time/project не теряются, смысловые строки добавляются сверху.
Косинус приходит из БД колонкой `vec_sim` — JSONB не разбирается.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from vera_shared.db import vectors
from vera_shared.db.vectors import VEC_TYPE, as_pg_vector

#: Сколько грубых кандидатов берём из индекса по Хэммингу перед точным
#: пересчётом. Замер на случайной выборке прода 2026-09-13 (voyage-4,
#: recall@10 после пересчёта против точного перебора, 60 запросов):
#:   корпус 1 тыс.:   50 → 0.995, 100 → 1.0
#:   корпус 3.7 тыс.: 50 → 0.965, 100 → 0.992, 200 → 1.0
#: Нужный запас растёт с корпусом, а на проде он в 120 раз больше выборки,
#: поэтому берём потолок ef_search pgvector — 1000. Цена — ~1000 чтений
#: строк по 2 КБ на запрос (~100-200 мс холодным), на фоне LLM-синтеза в
#: секунды это ничто. Фактический recall на всём корпусе —
#: `backfill_pgvector.py --recall`.
ANN_OVERSAMPLE = 1000
#: Сколько смысловых строк добавляется к основной выборке.
ANN_POOL = 200


def q_cast() -> str:
    return f"CAST(:q AS {VEC_TYPE}({vectors.VEC_DIMS}))"


def vec_columns() -> tuple[str, str]:
    """(колонка эмбеддинга, хвост с vec_sim), когда есть halfvec: косинус
    считает Postgres, а JSONB отдаётся только строкам, которые бэкфил ещё не
    прошёл — так частично залитая колонка не теряет им сходство."""
    return ("CASE WHEN ee.embedding_vec IS NULL THEN ee.embedding END AS embedding",
            f", 1 - (ee.embedding_vec <=> {q_cast()}) AS vec_sim")


def ann_rows_sql(where: str) -> Any:
    """Строки той же формы, что у retrieval._select: id…importance, embedding,
    rank, account, acc_match — плюс vec_sim."""
    inner = vectors.ann_candidates_sql(dims=vectors.VEC_DIMS, where=where,
                                       join_events=True)
    return text(f"""
        WITH ann AS ({inner})
        SELECT events.id, events.source, events.source_event_id,
               events.occurred_at, events.content_text, events.importance,
               NULL AS embedding, 0.0 AS rank, events.account,
               FALSE AS acc_match, ann.sim AS vec_sim
        FROM ann JOIN events ON events.id = ann.event_id
        ORDER BY ann.sim DESC
    """)


def ann_params(q_vec: list[float]) -> dict[str, Any]:
    return {"q": as_pg_vector(q_vec), "ann_k": ANN_OVERSAMPLE, "ann_top": ANN_POOL}


async def fetch_ann_rows(session: AsyncSession, q_vec: list[float],
                         where: str, params: dict[str, Any]) -> list[Any]:
    """`where` — тот же фильтр, что у основной выборки (без FTS-условия)."""
    await session.execute(vectors.ANN_SETTINGS_SQL,
                          vectors.ann_settings_params(ANN_OVERSAMPLE))
    stmt = ann_rows_sql(where)
    return list((await session.execute(stmt, {**params, **ann_params(q_vec)})).all())


def merge_candidates(primary: list[Any], semantic: list[Any]) -> list[Any]:
    """Основная выборка первой, смысловые — только новые id. Порядок тут
    ничего не решает (переранжирует scoring), важна полнота без дублей."""
    seen = {r[0] for r in primary}
    return list(primary) + [r for r in semantic if r[0] not in seen]
