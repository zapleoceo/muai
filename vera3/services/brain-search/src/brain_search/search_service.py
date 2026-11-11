"""Маршрутизация вопроса `/search` по веткам: отчёт, точные id, цитата,
время, эмбеддинг — и честный отказ, когда точного попадания нет.

HTTP здесь нет: маршрут в app.py только проверяет секрет, превращает
`InvalidSearchFilters` в 422 и отдаёт результат.
"""
from __future__ import annotations

import logging
from datetime import datetime

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
    explicit_title,
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
from brain_search.retrieval import Candidates, LinkScope, fetch_candidates
from brain_search.synthesis import answer as synthesize

log = logging.getLogger(__name__)

#: «саммари/что сделано/вытяни всё» → нужна ШИРОКАЯ выборка, иначе полсотни
#: рабочих сообщений не влезают в top-15.
SUMMARY_MIN_LIMIT = 60

_EVENT_MISSING = ("Указанное событие не найдено среди доступных записей. Это не "
                  "доказывает, что события не существует в источнике или что поиск "
                  "охватывает все данные.")
_TITLE_MISSING = ("Письмо с указанной точной темой не найдено среди доступных записей "
                  "за этот период. Это не доказывает, что письма нет в исходном ящике "
                  "или вне выбранного периода.")
_QUOTE_MISSING = ("Поиск по указанной цитате не вернул доступных записей. Это "
                  "ограниченная поисковая выборка; она не доказывает отсутствие "
                  "цитаты в исходных сообщениях.")


class InvalidSearchFilters(ValueError):
    """Фильтр запроса не разобрался — маршрут отвечает 422."""


async def try_report(question: str) -> AnswerResponse | None:
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


async def link_scope(query: SearchQuery) -> LinkScope | None:
    """Фильтр запроса → условие поиска; неверный фильтр — InvalidSearchFilters."""
    if not query.filters:
        return None
    try:
        flt = from_dict(query.filters)
        owner = await owner_entity_id() if flt.with_owner else None
        build_where(flt, owner)         # проверка заранее: ошибка до тяжёлой выборки
    except (FilterError, ValueError, TypeError) as e:
        raise InvalidSearchFilters(str(e)) from e
    return LinkScope(flt, owner)


def _refusal(answer: str) -> AnswerResponse:
    return AnswerResponse(answer=answer, results=[], provider=None, cost_usd=0)


def _no_exact_hit(question: str, found: Candidates, *, exact_ids: list[int],
                  identifiers: list[str], title: str | None,
                  time_range: tuple[datetime, datetime] | None,
                  phrase: str | None) -> AnswerResponse | None:
    """Точный запрос без точного попадания: честный отказ вместо синтеза по шуму."""
    if is_identifier_only(question, identifiers) and not has_exact_rows(found.rows):
        return _refusal(NO_EXACT_ANSWER)
    if exact_ids and not found.rows:
        return _refusal(_EVENT_MISSING)
    if title and time_range and not exact_ids and not found.title_found:
        return _refusal(_TITLE_MISSING)
    if phrase and not found.rows:
        return _refusal(_QUOTE_MISSING)
    return None


async def run_search(query: SearchQuery) -> AnswerResponse:
    """Гибридный поиск + LLM-синтез ответа."""
    exact_ids = explicit_event_ids(query.q)
    report = None if exact_ids else await try_report(query.q)
    if report is not None:
        return report

    focus, _scope = split_quoted_query(query.q)
    phrase = focus if focus != query.q else None
    q_vec = await embed_query(query.q) if not (exact_ids or phrase) else None
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
    title = explicit_title(query.q)
    found = await fetch_candidates(
        ts_query=ts, acc_words=acc_words,
        time_range=None if exact_ids else time_range,
        project=None if exact_ids else project,
        q_vec=q_vec, limit=eff_limit, links=await link_scope(query),
        exact_event_ids=exact_ids, tickets=identifiers,
        title=title, phrase=phrase,
    )
    refusal = _no_exact_hit(query.q, found, exact_ids=exact_ids, identifiers=identifiers,
                            title=title, time_range=time_range, phrase=phrase)
    if refusal is not None:
        return refusal

    return await synthesize(
        query.model_copy(update={"use_agent": False}) if phrase else query,
        found.rows, q_vec,
        acc_words=found.acc_words, summary=summary, eff_limit=eff_limit,
        project=project.name if project and not exact_ids else None,
    )
