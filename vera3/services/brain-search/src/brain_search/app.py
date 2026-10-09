"""Vera 3.0 search service — HTTP-слой.

Здесь только маршруты и разбор запроса. Всё остальное разъехалось по
соседям, потому что в одном файле на 530 строк жили сразу: роутинг, своя
копия проверки секрета, семь pydantic-моделей, ШЕСТЬ почти одинаковых
SELECT'ов, алгоритм скоринга, кэш самоописания со своим SQL и сборка
промпта с русским текстом внутри. Конвенция проекта — «один файл = одна
ответственность, потолок ~200 строк».

    models.py        формы запроса/ответа
    lang.py          служебные слова ru/uk/en/id для tsquery
    pipeline.py      вопрос → кандидаты → скоринг (общий с агентом)
    retrieval.py     выборка кандидатов (один запрос вместо шести копий)
    scoring.py       ранжирование
    self_context.py  «кто я и что подключено» + кэш
    synthesis.py     промпт и получение ответа (агент или прямой синтез)
    reports.py       точная SQL-агрегация вместо пересказа top-N
"""
from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from vera_shared.auth import internal_secret_ok
from vera_shared.db.engine import close_engine, init_engine
from vera_shared.links.context import owner_entity_id
from vera_shared.links.filters import FilterError, build_where, from_dict

from brain_search.identifier_rows import has_exact_rows
from brain_search.identifiers import (
    NO_EXACT_ANSWER,
    exact_identifiers,
    is_identifier_only,
)
from brain_search.models import AnswerResponse, SearchQuery
from brain_search.pipeline import embed_query, explicit_event_ids, query_terms
from brain_search.query_parse import (
    is_summary_query,
    parse_time_range,
    resolve_project,
)
from brain_search.quoted_query import split_quoted_query
from brain_search.reports import (
    build_monthly_report,
    detect_report_request,
    detect_target_field,
    find_report_chat,
    render_report_markdown,
    render_simple_markdown,
)
from brain_search.retrieval import LinkScope, fetch_candidates
from brain_search.synthesis import answer as synthesize

log = logging.getLogger(__name__)

#: «саммари/что сделано/вытяни всё» → нужна ШИРОКАЯ выборка, иначе полсотни
#: рабочих сообщений не влезают в top-15.
SUMMARY_MIN_LIMIT = 60


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


async def _try_report(question: str) -> AnswerResponse | None:
    """«Отчёт помесячно за <год>» по конкретному чату — точная SQL-агрегация
    ВСЕХ сообщений периода, а не пересказ top-N LLM'ом (см. reports.py).
    Без chat-match не перехватываем: обычный путь справится сам."""
    _focus, scope = split_quoted_query(question)
    wants, year = detect_report_request(scope)
    if not wants:
        return None
    match = await find_report_chat(scope)
    if not match:
        return None
    chat_id, chat_title = match
    report = await build_monthly_report(chat_id, chat_title, year)
    field = detect_target_field(scope)
    log.info("Report: chat=%s year=%s messages=%d field=%s",
             chat_title, year, report["total_messages"], field)
    return AnswerResponse(
        answer=(render_simple_markdown(report, field) if field
                else render_report_markdown(report)),
        results=[], provider="vera-report (точный расчёт, без LLM)",
        cost_usd=0.0,
    )


async def _link_scope(query: SearchQuery) -> LinkScope | None:
    """Фильтр запроса → условие поиска; неверный фильтр — 422."""
    if not query.filters:
        return None
    try:
        flt = from_dict(query.filters)
        owner = await owner_entity_id() if flt.with_owner else None
        build_where(flt, owner)         # проверка заранее: ошибка до тяжёлой выборки
    except (FilterError, ValueError, TypeError) as e:
        raise HTTPException(422, f"filters: {e}") from e
    return LinkScope(flt, owner)


@app.post("/search", response_model=AnswerResponse)
async def search(
    query: SearchQuery,
    x_internal_secret: str | None = Header(default=None),
) -> AnswerResponse:
    """Гибридный поиск + LLM-синтез ответа."""
    check_internal_secret(x_internal_secret)

    exact_ids = explicit_event_ids(query.q)
    report = None if exact_ids else await _try_report(query.q)
    if report is not None:
        return report

    q_vec = await embed_query(query.q) if not exact_ids else None
    time_range = parse_time_range(query.q)
    if time_range:
        # DEBUG, не INFO — query.q содержит текст вопроса Димы (может нести
        # личные детали), не должен оседать в INFO-логах контейнера.
        log.debug("Temporal filter: %s → [%s, %s)", query.q[:60], *time_range)

    # «по проекту Itstep» → реальные ящики + рабочие чаты, не текст «itstep»
    project = resolve_project(query.q)
    ts, acc_words = query_terms(query.q, project)
    summary = False if exact_ids else is_summary_query(query.q)
    eff_limit = max(query.limit, SUMMARY_MIN_LIMIT) if summary else query.limit

    identifiers = exact_identifiers(query.q)
    found = await fetch_candidates(
        ts_query=ts, acc_words=acc_words, time_range=time_range,
        project=project, q_vec=q_vec, limit=eff_limit, links=await _link_scope(query),
        exact_event_ids=exact_ids, tickets=identifiers,
    )

    if is_identifier_only(query.q, identifiers) and not has_exact_rows(found.rows):
        return AnswerResponse(answer=NO_EXACT_ANSWER, results=[], provider=None, cost_usd=0)

    if exact_ids and not found.rows:
        return AnswerResponse(
            answer="Указанное событие не найдено среди доступных записей. Это не доказывает, что события не существует в источнике или что поиск охватывает все данные.",
            results=[], provider=None, cost_usd=0,
        )

    focus, _scope = split_quoted_query(query.q)
    if focus != query.q and not found.rows and not query.use_agent:
        return AnswerResponse(
            answer="Поиск по указанной цитате не вернул доступных записей. Это ограниченная поисковая выборка; она не доказывает отсутствие цитаты в исходных сообщениях.",
            results=[], provider=None, cost_usd=0,
        )

    return await synthesize(
        query, found.rows, q_vec,
        acc_words=found.acc_words, summary=summary, eff_limit=eff_limit,
        project=project.name if project else None,
    )
