"""Vera 3.0 search service — HTTP-слой.

Здесь только маршруты и разбор запроса. Всё остальное разъехалось по
соседям, потому что в одном файле на 530 строк жили сразу: роутинг, своя
копия проверки секрета, семь pydantic-моделей, ШЕСТЬ почти одинаковых
SELECT'ов, алгоритм скоринга, кэш самоописания со своим SQL и сборка
промпта с русским текстом внутри. Конвенция проекта — «один файл = одна
ответственность, потолок ~200 строк».

    models.py        формы запроса/ответа
    retrieval.py     выборка кандидатов (один запрос вместо шести копий)
    scoring.py       ранжирование
    self_context.py  «кто я и что подключено» + кэш
    synthesis.py     промпт и получение ответа (агент или прямой синтез)
    reports.py       точная SQL-агрегация вместо пересказа top-N
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from vera_shared.auth import internal_secret_ok
from vera_shared.db.engine import close_engine, init_engine
from vera_shared.llm.client import LLMCallFailed, embed

from brain_search.models import AnswerResponse, SearchQuery
from brain_search.query_parse import (
    extract_account_terms,
    is_summary_query,
    parse_time_range,
    resolve_project,
)
from brain_search.reports import (
    build_monthly_report,
    detect_report_request,
    detect_target_field,
    find_report_chat,
    render_report_markdown,
    render_simple_markdown,
)
from brain_search.retrieval import fetch_candidates
from brain_search.synthesis import answer as synthesize

log = logging.getLogger(__name__)

# Стопслова и regex — module-level, не пересоздавать на каждый запрос
STOPWORDS = {
    "что", "как", "и", "в", "на", "о", "по", "у", "для", "это", "что-то",
    "ли", "ну", "же", "то", "был", "была", "были", "быть", "есть",
    "не", "ни", "при", "из", "за", "ты", "я", "мне", "мы", "вы",
    "он", "она", "они",
}
_WORD_RE = re.compile(r"[\wа-яА-ЯёЁ]+")

#: «саммари/что сделано/вытяни всё» → нужна ШИРОКАЯ выборка, иначе полсотни
#: рабочих сообщений не влезают в top-15.
SUMMARY_MIN_LIMIT = 60
EMBED_TIMEOUT_S = 15


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_engine()
    log.info("brain-search started")
    yield
    await close_engine()


app = FastAPI(title="Vera 3.0 Search", version="0.3.0", lifespan=lifespan)


def check_internal_secret(provided: str | None) -> None:
    """Fail-closed, как gateway.auth: нет настроенного секрета = нет доступа.
    Порт 8002 опубликован на 127.0.0.1 хоста — /search не должен быть открыт
    любому локальному процессу без секрета.

    Env читается на каждый вызов, а не на импорте: тесты подменяют его
    monkeypatch'ем, а служба всё равно живёт в контейнере с фиксированным
    окружением, так что дешевизна тут не важна."""
    if not internal_secret_ok(provided, os.environ.get("INTERNAL_SECRET", "")):
        raise HTTPException(401, "invalid internal secret")


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "brain-search"}


def _ts_query(question: str) -> tuple[str, list[str]]:
    """Postgres FTS с русским стеммером + имена собственные для матча по
    account. Возвращает (tsquery, слова-кандидаты в account)."""
    raw = _WORD_RE.findall(question)
    words = [w for w in raw if len(w) >= 2 and w.lower() not in STOPWORDS]
    ts = " | ".join(f"{w}:*" for w in words) if words else ""
    return ts, extract_account_terms(words)


async def _embed_query(question: str) -> list[float] | None:
    """Вектор запроса. Отказ брокера не фатален — остаётся FTS."""
    try:
        vecs = await asyncio.wait_for(embed([question]), timeout=EMBED_TIMEOUT_S)
    except (LLMCallFailed, asyncio.TimeoutError) as e:
        log.warning("Embed failed: %s — fallback only FTS", e)
        return None
    return vecs[0] if vecs else None


async def _try_report(question: str) -> AnswerResponse | None:
    """«Отчёт помесячно за <год>» по конкретному чату — точная SQL-агрегация
    ВСЕХ сообщений периода, а не пересказ top-N LLM'ом (см. reports.py).
    Без chat-match не перехватываем: обычный путь справится сам."""
    wants, year = detect_report_request(question)
    if not wants:
        return None
    match = await find_report_chat(question)
    if not match:
        return None
    chat_id, chat_title = match
    report = await build_monthly_report(chat_id, chat_title, year)
    field = detect_target_field(question)
    log.info("Report: chat=%s year=%s messages=%d field=%s",
             chat_title, year, report["total_messages"], field)
    return AnswerResponse(
        answer=(render_simple_markdown(report, field) if field
                else render_report_markdown(report)),
        results=[], provider="vera-report (точный расчёт, без LLM)",
        cost_usd=0.0,
    )


@app.post("/search", response_model=AnswerResponse)
async def search(
    query: SearchQuery,
    x_internal_secret: str | None = Header(default=None),
) -> AnswerResponse:
    """Гибридный поиск + LLM-синтез ответа."""
    check_internal_secret(x_internal_secret)

    report = await _try_report(query.q)
    if report is not None:
        return report

    q_vec = await _embed_query(query.q)
    ts, acc_words = _ts_query(query.q)
    time_range = parse_time_range(query.q)
    if time_range:
        # DEBUG, не INFO — query.q содержит текст вопроса Димы (может нести
        # личные детали), не должен оседать в INFO-логах контейнера.
        log.debug("Temporal filter: %s → [%s, %s)", query.q[:60], *time_range)

    # «по проекту Itstep» → реальные ящики + рабочие чаты, не текст «itstep»
    project = resolve_project(query.q)
    summary = is_summary_query(query.q)
    eff_limit = max(query.limit, SUMMARY_MIN_LIMIT) if summary else query.limit

    found = await fetch_candidates(
        ts_query=ts, acc_words=acc_words, time_range=time_range,
        project=project, has_vector=q_vec is not None, limit=eff_limit,
    )

    return await synthesize(
        query, found.rows, q_vec,
        acc_words=found.acc_words, summary=summary, eff_limit=eff_limit,
        project=project.name if project else None,
    )
