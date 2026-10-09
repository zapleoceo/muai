"""Сборка промпта и получение ответа — либо агентом, либо прямым синтезом."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from vera_shared.db.engine import get_session
from vera_shared.events.visibility import NOT_HIDDEN_SQL
from vera_shared.llm.client import LLMCallFailed, chat_async

from brain_search.agent import run_agent
from brain_search.authorship import AUTHORSHIP_RULES, author_tag
from brain_search.evidence import (
    EVIDENCE_RULES,
    PRIMARY_CHARS,
    SECONDARY_CHARS,
    evidence_excerpt,
)
from brain_search.identifiers import (
    exact_identifiers,
    identifier_hits,
    identifier_miss_note,
    identifier_note,
)
from brain_search.models import AnswerResponse, HistoryItem, SearchQuery, SearchResult
from brain_search.query_parse import SOURCE_PROMPT_NOTE
from brain_search.rows import Candidate
from brain_search.scoring import score_rows
from brain_search.self_context import self_context

log = logging.getLogger(__name__)

#: Сколько событий кладём в промпт. В режиме сводки больше — иначе полсотни
#: рабочих сообщений не влезают и синтез выходит куцым.
CONTEXT_EVENTS = 10
CONTEXT_EVENTS_SUMMARY = 30

_SUMMARY_NOTE = (
    "\n\nЭто запрос на СВОДКУ. Синтезируй ПО СУТИ: сгруппируй по темам "
    "(должники, расписание/группы, найм, продажи, переписка с командой), "
    "укажи факты и имена. НЕ перечисляй запросы Димы к тебе как «сделанное». "
    "Если событий мало — скажи что день ещё не закончен / данных пока мало, "
    "но НЕ выдумывай."
)


async def fetch_conversation_history(chat_id: int,
                                     limit_pairs: int = 8) -> list[HistoryItem]:
    """Последние N пар user/vera из events (source='vera_chat').

    Из БД, а не из памяти процесса: контекст переживает рестарт бота,
    доступен с любого устройства и сам попадает в FTS.
    """
    async with get_session() as s:
        rows = (await s.execute(text(f"""
            SELECT category, content_text, occurred_at
            FROM events
            WHERE source = 'vera_chat' AND {NOT_HIDDEN_SQL}
              AND (metadata->>'chat_id')::bigint = :chat_id
              AND content_text != ''
            ORDER BY occurred_at DESC
            LIMIT :n
        """), {"chat_id": chat_id, "n": limit_pairs * 2})).all()
    # выборка шла DESC — возвращаем в хронологическом порядке
    return [HistoryItem(role=r[0], content=r[1]) for r in reversed(rows)]


def _history_block(history: list[HistoryItem], question: str) -> str:
    recent = [h for h in history if h.content.strip() != question.strip()]
    if not recent:
        return ""
    lines = ["### Предыдущий разговор (понимай контекст уточняющих вопросов):"]
    for h in recent[-16:]:
        who = "Дима" if h.role == "user" else "Вера (ты)"
        lines.append(f"{who}: {h.content[:600]}")
    return "\n".join(lines) + "\n\n"


def build_prompt(*, question: str, self_ctx: str, context: str,
                 history_block: str, notes: str) -> str:
    """Стабильные инструкции спереди (prompt cache), данные запроса и notes — в хвосте."""
    return (
        "Ты — Вера, личная память Димы.\n\n"
        "ВАЖНО:\n"
        "1) Учитывай предыдущий разговор для уточняющих вопросов.\n"
        "2) Если вопрос про ТЕБЯ саму — отвечай по «Твоя конфигурация».\n"
        "3) Если вопрос про факты/события — отвечай по найденным событиям. "
        "Если данных нет — честно скажи.\n"
        "4) Каждое существенное утверждение о событии сопровождай [event:ID] "
        "из найденных событий. Если у события есть ссылка на оригинал, "
        "приведи её рядом; если ссылки нет, не выдумывай её.\n"
        f"{AUTHORSHIP_RULES}{EVIDENCE_RULES}{SOURCE_PROMPT_NOTE}\n"
        f"### Твоя конфигурация (твоя реальная, не из писем!)\n{self_ctx}\n\n"
        f"{history_block}"
        f"### Текущий вопрос Димы:\n{question}\n\n"
        f"### Найденные события:\n{context}\n"
        f"{notes}"
    )


def build_context(results: list[SearchResult], by_id: dict[int, Candidate]) -> str:
    """Блоки событий; автор и направление — из metadata, не из владельца ящика."""
    blocks = []
    for i, r in enumerate(results):
        c = by_id[r.event_id]
        tag = author_tag(c)
        limit = PRIMARY_CHARS if i < 3 else SECONDARY_CHARS
        blocks.append(
            f"[event:{r.event_id} | {r.occurred_at[:16]} UTC | {r.source} | "
            f"{r.source_url or 'original link unavailable'}"
            f"{' | ' + tag if tag else ''}] "
            f"{evidence_excerpt(c.content_text, limit)}")
    return "\n\n".join(blocks) if blocks else "(нет данных)"


async def answer(
    query: SearchQuery, rows, q_vec, *,
    acc_words: list[str] | None = None,
    summary: bool = False,
    eff_limit: int | None = None,
    project: str | None = None,
) -> AnswerResponse:
    """Скоринг → промпт → ответ. Общий хвост всех retrieval-веток."""
    candidates = score_rows(rows, q_vec, acc_words or [])
    top = candidates[:(eff_limit or query.limit)]
    results = [SearchResult(score=score, **info) for score, info in top]
    full_text_by_id = {c.id: c.content_text for c in (Candidate.of(row) for row in rows)}

    self_ctx = await self_context()
    ctx_n = CONTEXT_EVENTS_SUMMARY if summary else CONTEXT_EVENTS
    context = build_context(results[:ctx_n], {c.id: c for c in map(Candidate.of, rows)})
    identifiers = exact_identifiers(query.q)
    hits = identifier_hits(identifiers,
                           {r.event_id: full_text_by_id[r.event_id] for r in results})

    history: list[HistoryItem] = []
    if query.conversation:
        history = await fetch_conversation_history(query.conversation.chat_id)
    if not history and query.history:
        history = list(query.history)
    history_block = _history_block(history, query.q)

    notes = (_SUMMARY_NOTE if summary else "") + identifier_note(hits) + identifier_miss_note(identifiers, hits)
    if project:
        notes += (f"\n\nВопрос про проект «{project}». Все события ниже уже "
                  f"отобраны как относящиеся к нему (рабочие ящики + чаты). "
                  f"Отвечай по ним.")

    # project/summary: события уже отобраны точно (account+chats / окно).
    # Агенту нечего доискивать — он только зациклится, прямой синтез надёжнее.
    if query.use_agent and not project and not summary:
        trace = await run_agent(
            user_query=query.q,
            initial_context=context,
            self_context=self_ctx,
            history_block=history_block + notes,
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

    prompt = build_prompt(question=query.q, self_ctx=self_ctx, context=context,
                          history_block=history_block, notes=notes)
    try:
        answer_text, meta = await asyncio.wait_for(
            chat_async(messages=[{"role": "user", "content": prompt}],
                       capability="chat:smart", max_tokens=900,
                       temperature=0.5, workflow="search"),
            timeout=90,
        )
        provider, cost = meta.get("provider"), meta.get("cost_usd", 0.0)
    except (LLMCallFailed, asyncio.TimeoutError) as e:
        log.warning("Synth failed: %s", e)
        answer_text = ("Не могу обратиться к LLM (провайдеры заняты или "
                       f"таймаут). Нашёл {len(results)} событий.")
        provider, cost = None, 0.0

    return AnswerResponse(answer=answer_text, results=results,
                          provider=provider, cost_usd=cost,
                          history_used=len(history))
