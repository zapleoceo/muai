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
from vera_shared.db.models_sources import GmailAccountRow
from vera_shared.llm.client import LLMCallFailed, chat, embed

from brain_search.agent import run_agent

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_engine()
    log.info("brain-search started")
    yield
    await close_engine()


app = FastAPI(title="Vera 3.0 Search", version="0.3.0", lifespan=lifespan)


class ConversationCtx(BaseModel):
    """Идентификатор разговора — для retrieval recent vera_chat events."""
    chat_id: int
    user_id: int | None = None


class HistoryItem(BaseModel):
    role: str  # "user" | "vera"
    content: str


class SearchQuery(BaseModel):
    q: str = Field(min_length=1)
    limit: int = 15
    days_back: int | None = None
    # Прямая передача истории (legacy/dashboard)
    history: list[HistoryItem] = Field(default_factory=list)
    # Новый правильный путь — bot передаёт только chat_id, search сам тянет из БД
    conversation: ConversationCtx | None = None
    # Включить ReAct agent loop с tool calling. По умолчанию ON.
    use_agent: bool = True
    max_steps: int = 6


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
    history_used: int = 0
    agent_steps: int = 0
    agent_trace: list[dict[str, Any]] = Field(default_factory=list)


async def _fetch_conversation_history(chat_id: int, limit_pairs: int = 8) -> list[HistoryItem]:
    """Достаём последние N пар user/vera из events table source='vera_chat'.

    Это правильный способ — никаких in-memory кешей. Контекст переживает
    рестарт бота, доступен с любого устройства, и сам индексируется FTS.
    """
    n = limit_pairs * 2  # пар → отдельных реплик
    async with get_session() as s:
        stmt = text("""
            SELECT category, content_text, occurred_at
            FROM events
            WHERE source = 'vera_chat'
              AND (metadata->>'chat_id')::bigint = :chat_id
              AND content_text != ''
            ORDER BY occurred_at DESC
            LIMIT :n
        """)
        rows = (await s.execute(stmt, {"chat_id": chat_id, "n": n})).all()

    # rows in DESC order — reverse to chronological
    rows = list(reversed(rows))
    # category поле = role ('user' / 'vera')
    return [HistoryItem(role=r[0], content=r[1]) for r in rows]


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


async def _self_context() -> str:
    """Описание реальной конфигурации Веры — что подключено, сколько данных.

    Включается в каждый search-prompt, чтобы Вера могла ответить про себя.
    """
    from sqlalchemy import func
    async with get_session() as s:
        gmail_accs = (await s.execute(
            select(GmailAccountRow.email)
            .where(GmailAccountRow.is_active.is_(True))
            .order_by(GmailAccountRow.id)
        )).scalars().all()

        total_events = (await s.execute(
            select(func.count(EventRow.id))
        )).scalar() or 0

        per_src = (await s.execute(text(
            "SELECT source, COUNT(*) FROM events GROUP BY source ORDER BY 2 DESC"
        ))).all()

    lines = ["Я — Vera 3.0, личная память Димы.",
             f"Всего событий в моём мозге: {total_events:,}.",
             "",
             "Подключённые ИСТОЧНИКИ (по которым я реально читаю входящий поток):"]

    # Gmail accounts (live source)
    if gmail_accs:
        lines.append(f"• Gmail (через OAuth) — {len(gmail_accs)} ящика:")
        for email in gmail_accs:
            lines.append(f"  – {email}")
    else:
        lines.append("• Gmail — нет подключённых ящиков.")

    # Show actual breakdown of events by source — what's accumulated
    lines.append("")
    lines.append("По источникам в БД событий (накоплено за всё время):")
    for src, cnt in per_src:
        lines.append(f"• {src}: {cnt:,}")

    return "\n".join(lines)


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

    # 4. Self-awareness: подключённые источники
    self_ctx = await _self_context()

    # 5. Synthesize answer
    context_blocks = []
    for r in results[:10]:
        context_blocks.append(
            f"[{r.occurred_at[:10]} | {r.source}] {r.content_preview[:300]}"
        )
    context = "\n\n".join(context_blocks) if context_blocks else "(нет данных)"

    # Conversation history — правильный путь: тянем из БД по chat_id
    # (не in-memory, переживает рестарты, доступно с любого канала)
    history: list[HistoryItem] = []
    if query.conversation:
        history = await _fetch_conversation_history(query.conversation.chat_id, limit_pairs=8)
    # Legacy путь: явный history в payload (для dashboard и debugging)
    if not history and query.history:
        history = list(query.history)

    history_block = ""
    if history:
        # Последняя реплика в БД может быть текущим вопросом — отсечём её
        # (бот пишет user-event ДО search-вызова)
        recent = [h for h in history if h.content.strip() != query.q.strip()]
        if recent:
            lines = ["### Предыдущий разговор (понимай контекст уточняющих вопросов):"]
            for h in recent[-16:]:
                who = "Дима" if h.role == "user" else "Вера (ты)"
                lines.append(f"{who}: {h.content[:600]}")
            history_block = "\n".join(lines) + "\n\n"

    synth_prompt = (
        "Ты — Вера, личная память Димы.\n\n"
        f"### Твоя конфигурация (твоя реальная, не из писем!)\n{self_ctx}\n\n"
        f"{history_block}"
        f"### Текущий вопрос Димы:\n{query.q}\n\n"
        f"### Найденные события (топ-10 из истории):\n{context}\n\n"
        "ВАЖНО:\n"
        "1) Учитывай предыдущий разговор. Если вопрос похож на уточняющий "
        "(«а ещё?», «расскажи подробнее», «а другие?», местоимения «он/она/они/это») — "
        "связывай с тем что ты уже сказала. Не отвечай «нет данных» если "
        "в прошлом ответе ты что-то перечислила и Дима спрашивает про продолжение.\n"
        "2) Если вопрос про ТЕБЯ саму (источники, почты, чаты, кто ты) — отвечай "
        "по разделу «Твоя конфигурация», НЕ по найденным событиям. Email отправителей "
        "в письмах ≠ твои подключённые ящики.\n"
        "3) Если вопрос про факты/людей/события — отвечай по найденным событиям. "
        "Если данных нет — честно скажи."
    )

    # Agent loop путь (по умолчанию) — позволяет LLM звать tools и обогащать ответ
    if query.use_agent:
        trace = await run_agent(
            user_query=query.q,
            initial_context=context,
            self_context=self_ctx,
            history_block=history_block,
            max_steps=query.max_steps,
        )
        return AnswerResponse(
            answer=trace.answer or "(пусто)",
            results=results,
            provider=trace.provider_last,
            cost_usd=trace.cost_usd,
            history_used=len(history),
            agent_steps=trace.final_step or len(trace.steps),
            agent_trace=trace.steps,
        )

    # Legacy однопроход (use_agent=false) — для отладки/dashboard.
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
        history_used=len(history),
    )
