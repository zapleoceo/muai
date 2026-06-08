"""Vera 3.0 search service — hybrid retrieval + answer synthesis."""
from __future__ import annotations

import logging
import math
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, select, text

from vera_shared.db.engine import close_engine, get_session, init_engine
from vera_shared.db.models import EventRow
from vera_shared.llm.client import LLMCallFailed, chat, embed

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_engine()
    log.info("brain-search started")
    yield
    await close_engine()


app = FastAPI(title="Vera 3.0 Search", version="0.3.0", lifespan=lifespan)


class SearchQuery(BaseModel):
    q: str = Field(min_length=1)
    limit: int = 15
    days_back: int | None = None


class SearchResult(BaseModel):
    event_id: int
    source: str
    occurred_at: str
    content_preview: str
    importance: int | None
    score: float


class AnswerResponse(BaseModel):
    answer: str
    results: list[SearchResult]
    provider: str | None
    cost_usd: float


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return s / (na * nb)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "brain-search"}


@app.post("/search", response_model=AnswerResponse)
async def search(query: SearchQuery) -> AnswerResponse:
    """Гибридный поиск + LLM-синтез ответа."""
    # 1. Embed запроса
    try:
        q_vecs = await embed(query.q)
        q_vec = q_vecs[0]
    except LLMCallFailed as e:
        log.warning("Embed failed: %s — fallback only FTS", e)
        q_vec = None

    # Postgres FTS с русским стеммером — учитывает морфологию И word boundaries.
    # Решает проблемы:
    #   - «виза» матчит «визой/визу/визы», но НЕ «Визардиум» (другой токен)
    #   - «Лизе» матчит «Лиза/Лизы/Лизой»
    # ts_rank даёт нативную релевантность.
    async with get_session() as s:
        # Удаляем стопслова + escape tsquery спецсимволы
        import re
        STOPWORDS = {"что", "как", "и", "в", "на", "о", "по", "у", "для",
                     "это", "что-то", "ли", "ну", "же", "то", "был", "была",
                     "были", "быть", "есть", "не", "ни", "при", "из", "за",
                     "ты", "я", "мне", "мы", "вы", "он", "она", "они"}
        raw_words = re.findall(r"[\wа-яА-ЯёЁ]+", query.q)
        words = [w for w in raw_words if len(w) >= 2 and w.lower() not in STOPWORDS]
        # to_tsquery с prefix-match (:* — найдёт «виза», «визу», «визой»)
        ts_query = " | ".join(f"{w}:*" for w in words) if words else ""

        if ts_query:
            stmt = text("""
                SELECT id, source, source_event_id, occurred_at, content_text,
                       importance, embedding_voyage_3,
                       ts_rank(to_tsvector('russian', content_text),
                               to_tsquery('russian', :tsq)) AS rank
                FROM events
                WHERE to_tsvector('russian', content_text)
                      @@ to_tsquery('russian', :tsq)
                ORDER BY rank DESC, occurred_at DESC
                LIMIT 200
            """)
            rs = (await s.execute(stmt, {"tsq": ts_query})).all()
        else:
            stmt = text(
                "SELECT id, source, source_event_id, occurred_at, content_text, "
                "importance, embedding_voyage_3, 0.0 AS rank "
                "FROM events ORDER BY occurred_at DESC LIMIT 100"
            )
            rs = (await s.execute(stmt)).all()

    candidates: list[tuple[float, dict]] = []
    for r in rs:
        # Hybrid score: FTS rank + semantic similarity + importance
        ts_rank = float(r[7]) if r[7] is not None else 0.0
        score = ts_rank * 2.0  # FTS — главный сигнал
        emb = r[6]
        if q_vec and emb:
            score += _cosine(q_vec, emb)
        if r[5]:
            score += r[5] / 200.0
        candidates.append((score, {
            "event_id": r[0],
            "source": r[1],
            "occurred_at": str(r[3]),
            "content_preview": (r[4] or "")[:400],
            "importance": r[5],
        }))

    candidates.sort(key=lambda x: x[0], reverse=True)
    top = candidates[: query.limit]

    results = [SearchResult(score=score, **info) for score, info in top]

    # 4. Synthesize answer
    context_blocks = []
    for r in results[:10]:
        context_blocks.append(
            f"[{r.occurred_at[:10]} | {r.source}] {r.content_preview[:300]}"
        )
    context = "\n\n".join(context_blocks) if context_blocks else "(нет данных)"

    synth_prompt = (
        "Ты — Вера, личная память Димы. Ответь на его вопрос на основе найденных событий.\n\n"
        f"Вопрос: {query.q}\n\n"
        f"Найденные события (топ-10 из истории):\n{context}\n\n"
        "Ответь кратко и по делу. Если данных недостаточно — скажи это честно. "
        "Используй имена/даты из событий. Без префиксов 'Согласно данным' — просто отвечай."
    )

    try:
        answer_text, meta = await chat(
            messages=[{"role": "user", "content": synth_prompt}],
            capability="chat:smart",
            max_tokens=800,
            temperature=0.5,
            workflow="search",
        )
        provider = meta.get("provider")
        cost = meta.get("cost_usd", 0.0)
    except LLMCallFailed:
        answer_text = f"Не могу обратиться к LLM (провайдеры заняты). Нашёл {len(results)} событий."
        provider = None
        cost = 0.0

    return AnswerResponse(
        answer=answer_text,
        results=results,
        provider=provider,
        cost_usd=cost,
    )
