"""Vera 3.0 search service — hybrid retrieval + answer synthesis."""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from vera_shared.db.engine import close_engine, get_session, init_engine
from vera_shared.db.models import EventRow
from vera_shared.db.models_sources import GmailAccountRow
from vera_shared.llm.client import LLMCallFailed, chat, embed

from brain_search.agent import run_agent
from brain_search.query_parse import (
    SOURCE_PROMPT_NOTE,
    extract_account_terms,
    is_summary_query,
    parse_time_range,
    resolve_project,
    source_weight,
)

log = logging.getLogger(__name__)

# Стопслова и regex — module-level, не пересоздавать на каждый запрос
STOPWORDS = {
    "что", "как", "и", "в", "на", "о", "по", "у", "для", "это", "что-то",
    "ли", "ну", "же", "то", "был", "была", "были", "быть", "есть",
    "не", "ни", "при", "из", "за", "ты", "я", "мне", "мы", "вы",
    "он", "она", "они",
}
_WORD_RE = re.compile(r"[\wа-яА-ЯёЁ]+")
# Self-context кэш — COUNT(*) FROM events это seq scan, не запускаем на каждый запрос
_SELF_CTX_TTL_S = 60
_self_ctx_cache: dict[str, Any] = {"value": None, "fetched_at": 0.0}


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
    """Описание реальной конфигурации Веры. Кэшируется на _SELF_CTX_TTL_S
    чтобы COUNT(*) FROM events не делался на каждый /search."""
    now = time.time()
    if _self_ctx_cache["value"] and (now - _self_ctx_cache["fetched_at"] < _SELF_CTX_TTL_S):
        return _self_ctx_cache["value"]

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

    result = "\n".join(lines)
    _self_ctx_cache["value"] = result
    _self_ctx_cache["fetched_at"] = now
    return result


@app.post("/search", response_model=AnswerResponse)
async def search(query: SearchQuery) -> AnswerResponse:
    """Гибридный поиск + LLM-синтез ответа."""
    # 1. Embed запроса — ВАЖНО передаём как list[str], НЕ str.
    # `embed(str)` сейчас правильно оборачивает в [str], но явный list безопаснее.
    q_vec: list[float] | None = None
    try:
        q_vecs = await asyncio.wait_for(embed([query.q]), timeout=15)
        q_vec = q_vecs[0] if q_vecs else None
    except (LLMCallFailed, asyncio.TimeoutError) as e:
        log.warning("Embed failed: %s — fallback only FTS", e)

    # Темпоральный фильтр: «вчера/сегодня/за неделю/9 июня» → WHERE occurred_at
    time_range = parse_time_range(query.q)
    time_where = ""
    time_params: dict[str, Any] = {}
    if time_range:
        time_where = " AND occurred_at >= :t_start AND occurred_at < :t_end"
        time_params = {"t_start": time_range[0], "t_end": time_range[1]}
        log.info("Temporal filter: %s → [%s, %s)", query.q[:60], *time_range)

    # «по проекту Itstep» → реальные ящики + рабочие чаты (не текст «itstep»)
    project = resolve_project(query.q)
    # «саммари/что сделано/вытяни всё» → нужна ШИРОКАЯ выборка, иначе 53
    # рабочих сообщения не влезут в top-15
    summary = is_summary_query(query.q)
    eff_limit = max(query.limit, 60) if summary else query.limit

    # Project-scoped retrieval. ПЕРВИЧНЫЙ сигнал — колонка project, которую
    # триаж проставляет сам по содержимому (системно). Реестр chats/accounts —
    # fallback для событий, ещё не классифицированных Верой.
    if project:
        conds = ["(nature IS NULL OR nature NOT IN ('conversation_with_me', 'my_intent'))",
                 "source <> 'vera_chat'"]
        pparams: dict[str, Any] = {"pname": project.name}
        ors = ["project = :pname"]
        for i, pat in enumerate(project.account_like):
            ors.append(f"account ILIKE :pacc{i}")
            pparams[f"pacc{i}"] = f"%{pat}%"
        if project.chats:
            ors.append("metadata->>'chat_title' = ANY(:pchats)")
            pparams["pchats"] = project.chats
        conds.append("(" + " OR ".join(ors) + ")")
        if time_range:
            conds.append("occurred_at >= :t_start AND occurred_at < :t_end")
            pparams.update(time_params)
        where_sql = " AND ".join(conds)
        async with get_session() as s:
            stmt = text(f"""
                SELECT id, source, source_event_id, occurred_at, content_text,
                       importance, embedding_voyage_3, 0.0 AS rank, account
                FROM events WHERE {where_sql}
                ORDER BY occurred_at DESC
                LIMIT :lim
            """)
            rs = (await s.execute(stmt, {**pparams, "lim": eff_limit})).all()
        log.info("Project=%s scope: %d events (summary=%s, range=%s)",
                 project.name, len(rs), summary, bool(time_range))
        return await _finish_search(query, rs, q_vec, summary=summary,
                                    eff_limit=eff_limit, project=project.name)

    # Postgres FTS с русским стеммером
    raw_words = _WORD_RE.findall(query.q)
    words = [w for w in raw_words if len(w) >= 2 and w.lower() not in STOPWORDS]
    ts_query = " | ".join(f"{w}:*" for w in words) if words else ""

    # Account-matching: «Itstep» живёт в account='zaporozec_d@itstep.org',
    # а письмо на английском — текстовый FTS его не найдёт. Берём только
    # имена собственные (латиница / с Заглавной) — маркеры проекта/бренда.
    acc_words = extract_account_terms(words)
    acc_where = ""
    acc_match_expr = "FALSE"
    acc_params: dict[str, Any] = {}
    if acc_words:
        ors = []
        for i, w in enumerate(acc_words):
            ors.append(f"account ILIKE :acc{i}")
            acc_params[f"acc{i}"] = f"%{w}%"
        acc_where = " OR " + " OR ".join(ors)
        acc_match_expr = "(" + " OR ".join(ors) + ")"

    # Разговоры с Верой — не «события мира»: системно по nature (триаж
    # классифицирует сам), source-фильтр остаётся для неклассифицированных.
    base_where = (" AND (nature IS NULL OR nature <> 'conversation_with_me')"
                  " AND source <> 'vera_chat'")

    async with get_session() as s:
        if ts_query:
            # acc_match DESC первым в ORDER — иначе account-совпадения с rank=0
            # (англ. письма) отрезаются LIMIT 200 в пользу FTS-матчей.
            stmt = text(f"""
                SELECT id, source, source_event_id, occurred_at, content_text,
                       importance, embedding_voyage_3,
                       ts_rank(to_tsvector('russian', content_text),
                               to_tsquery('russian', :tsq)) AS rank,
                       account,
                       {acc_match_expr} AS acc_match
                FROM events
                WHERE (to_tsvector('russian', content_text)
                      @@ to_tsquery('russian', :tsq){acc_where}){time_where}{base_where}
                ORDER BY acc_match DESC, rank DESC, occurred_at DESC
                LIMIT 200
            """)
            rs = (await s.execute(
                stmt, {"tsq": ts_query, **acc_params, **time_params})).all()
            if not rs and time_range:
                stmt = text(f"""
                    SELECT id, source, source_event_id, occurred_at, content_text,
                           importance, embedding_voyage_3, 0.0 AS rank, account
                    FROM events
                    WHERE 1=1{time_where}{base_where}
                    ORDER BY occurred_at DESC LIMIT 200
                """)
                rs = (await s.execute(stmt, time_params)).all()
        elif time_range:
            stmt = text(f"""
                SELECT id, source, source_event_id, occurred_at, content_text,
                       importance, embedding_voyage_3, 0.0 AS rank, account
                FROM events
                WHERE 1=1{time_where}{base_where}
                ORDER BY occurred_at DESC LIMIT 200
            """)
            rs = (await s.execute(stmt, time_params)).all()
        elif q_vec:
            stmt = text(f"""
                SELECT id, source, source_event_id, occurred_at, content_text,
                       importance, embedding_voyage_3, 0.0 AS rank, account
                FROM events
                WHERE embedding_voyage_3 IS NOT NULL{base_where}
                ORDER BY occurred_at DESC LIMIT 200
            """)
            rs = (await s.execute(stmt)).all()
        else:
            stmt = text(f"""
                SELECT id, source, source_event_id, occurred_at, content_text,
                       importance, embedding_voyage_3, 0.0 AS rank, account
                FROM events WHERE 1=1{base_where}
                ORDER BY occurred_at DESC LIMIT 30
            """)
            rs = (await s.execute(stmt)).all()

    return await _finish_search(query, rs, q_vec, acc_words=acc_words,
                                summary=summary, eff_limit=eff_limit)


def _score_rows(rs, q_vec, acc_words: list[str]) -> list[tuple[float, dict]]:
    candidates: list[tuple[float, dict]] = []
    for r in rs:
        ts_rank = float(r[7]) if r[7] is not None else 0.0
        score = ts_rank * 2.0
        emb = r[6]
        if q_vec and emb:
            score += _cosine(q_vec, emb)
        if r[5]:
            score += r[5] / 200.0
        account_l = (r[8] or "").lower() if len(r) > 8 else ""
        if account_l and any(w in account_l for w in acc_words):
            score += 1.0
        score *= source_weight(r[1])
        candidates.append((score, {
            "event_id": r[0],
            "source": r[1],
            "occurred_at": str(r[3]),
            "content_preview": (r[4] or "")[:400],
            "importance": r[5],
        }))
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates


async def _finish_search(
    query: SearchQuery, rs, q_vec, *,
    acc_words: list[str] | None = None,
    summary: bool = False,
    eff_limit: int | None = None,
    project: str | None = None,
) -> AnswerResponse:
    """Скоринг + synthesis. Общий хвост для всех retrieval-веток."""
    acc_words = acc_words or []
    candidates = _score_rows(rs, q_vec, acc_words)
    out_limit = eff_limit or query.limit
    top = candidates[:out_limit]
    results = [SearchResult(score=score, **info) for score, info in top]

    self_ctx = await _self_context()

    # В summary-режиме даём LLM больше событий (до 30), чтобы синтез был полным
    ctx_n = 30 if summary else 10
    context_blocks = [
        f"[{r.occurred_at[:16]} | {r.source}] {r.content_preview[:300]}"
        for r in results[:ctx_n]
    ]
    context = "\n\n".join(context_blocks) if context_blocks else "(нет данных)"

    history: list[HistoryItem] = []
    if query.conversation:
        history = await _fetch_conversation_history(query.conversation.chat_id, limit_pairs=8)
    if not history and query.history:
        history = list(query.history)

    history_block = ""
    if history:
        recent = [h for h in history if h.content.strip() != query.q.strip()]
        if recent:
            lines = ["### Предыдущий разговор (понимай контекст уточняющих вопросов):"]
            for h in recent[-16:]:
                who = "Дима" if h.role == "user" else "Вера (ты)"
                lines.append(f"{who}: {h.content[:600]}")
            history_block = "\n".join(lines) + "\n\n"

    summary_note = ""
    if summary:
        summary_note = (
            "\n\nЭто запрос на СВОДКУ. Синтезируй ПО СУТИ: сгруппируй по темам "
            "(должники, расписание/группы, найм, продажи, переписка с командой), "
            "укажи факты и имена. НЕ перечисляй запросы Димы к тебе как «сделанное». "
            "Если событий мало — скажи что день ещё не закончен / данных пока мало, "
            "но НЕ выдумывай."
        )
    project_note = ""
    if project:
        project_note = (
            f"\n\nВопрос про проект «{project}». Все события ниже уже отобраны как "
            f"относящиеся к нему (рабочие ящики + чаты). Отвечай по ним."
        )

    synth_prompt = (
        "Ты — Вера, личная память Димы.\n\n"
        f"### Твоя конфигурация (твоя реальная, не из писем!)\n{self_ctx}\n\n"
        f"{history_block}"
        f"### Текущий вопрос Димы:\n{query.q}\n\n"
        f"### Найденные события:\n{context}\n\n"
        "ВАЖНО:\n"
        "1) Учитывай предыдущий разговор для уточняющих вопросов.\n"
        "2) Если вопрос про ТЕБЯ саму — отвечай по «Твоя конфигурация».\n"
        "3) Если вопрос про факты/события — отвечай по найденным событиям. "
        "Если данных нет — честно скажи.\n"
        f"{SOURCE_PROMPT_NOTE}"
        f"{summary_note}{project_note}"
    )

    # project/summary: события уже отобраны точно (account+chats / time-window).
    # Агенту нечего доискивать — он только зациклится. Прямой синтез надёжнее.
    use_agent = query.use_agent and not project and not summary

    if use_agent:
        trace = await run_agent(
            user_query=query.q,
            initial_context=context,
            self_context=self_ctx,
            history_block=history_block + summary_note + project_note,
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

    try:
        answer_text, meta = await asyncio.wait_for(
            chat(
                messages=[{"role": "user", "content": synth_prompt}],
                capability="chat:smart",
                max_tokens=900,
                temperature=0.5,
                workflow="search",
            ),
            timeout=90,
        )
        provider = meta.get("provider")
        cost = meta.get("cost_usd", 0.0)
    except (LLMCallFailed, asyncio.TimeoutError) as e:
        log.warning("Synth failed: %s", e)
        answer_text = f"Не могу обратиться к LLM (провайдеры заняты или таймаут). Нашёл {len(results)} событий."
        provider = None
        cost = 0.0

    return AnswerResponse(
        answer=answer_text,
        results=results,
        provider=provider,
        cost_usd=cost,
        history_used=len(history),
    )
