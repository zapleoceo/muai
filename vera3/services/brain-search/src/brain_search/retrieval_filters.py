"""WHERE-фрагменты выборки кандидатов: проект, окно времени, account, источник."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from vera_shared.events.visibility import NOT_HIDDEN_SQL
from vera_shared.links.filters import EventFilter, and_clause
from vera_shared.timeutil import utc_naive_now

#: events.project ставит триаж уже после вставки; свежие события ещё без
#: проекта, и без этого окна проектный поиск не видел бы последние часы.
UNTRIAGED_GRACE = timedelta(days=2)

#: Разговоры с Верой — не «события мира». Системно по nature (её проставляет
#: триаж), source-фильтр остаётся для ещё не классифицированных.
NOT_A_WORLD_EVENT = (" AND (nature IS NULL OR nature <> 'conversation_with_me')"
                     " AND source <> 'vera_chat'"
                     f" AND {NOT_HIDDEN_SQL}")


@dataclass(frozen=True)
class LinkScope:
    """Фильтр по людям/виду/периоду (`vera_shared.links.filters`) и id владельца."""
    flt: EventFilter
    owner_id: int | None = None


def links_clause(links: LinkScope | None) -> tuple[str, dict[str, Any]]:
    """Дописка « AND …» к WHERE по `events`; пусто без фильтра. Условия — EXISTS по
    `event_entities`, поэтому кандидаты сужаются ДО ранжирования, а не после него."""
    if links is None:
        return "", {}
    return and_clause(links.flt, links.owner_id, "events")


def source_clause(source: str | None) -> tuple[str, dict[str, Any]]:
    """Ограничение по источнику (инструмент агента); None/«any» — без него."""
    if not source or source == "any":
        return "", {}
    return " AND source = :src", {"src": source}


def project_clause(project, time_range, source: str | None = None,
                   links: LinkScope | None = None) -> tuple[str, dict[str, Any]]:
    """WHERE для проектной выборки по колонке `project` — её проставляют триаж
    и sync_projects (правила: vera_shared.projects.rules); на проде она
    заполнена у 99.8% событий, поэтому ящики и названия чатов не дублируем."""
    conds = ["(nature IS NULL OR nature NOT IN ('conversation_with_me', 'my_intent'))",
             "source <> 'vera_chat'", NOT_HIDDEN_SQL,
             "(project = :pname OR (project IS NULL AND occurred_at > :fresh_after))"]
    params: dict[str, Any] = {"pname": project.name,
                              "fresh_after": utc_naive_now() - UNTRIAGED_GRACE}
    if time_range:
        conds.append("occurred_at >= :t_start AND occurred_at < :t_end")
        params["t_start"], params["t_end"] = time_range
    src_sql, src_params = source_clause(source)
    link_sql, link_params = links_clause(links)
    return " AND ".join(conds) + src_sql + link_sql, {**params, **src_params, **link_params}


def account_clause(acc_words: list[str]) -> tuple[str, str, dict[str, Any]]:
    """«Itstep» живёт в account='zaporozec_d@itstep.org', а письмо на
    английском текстовый FTS не найдёт. Возвращает (OR-хвост для WHERE,
    выражение для ORDER BY, параметры)."""
    if not acc_words:
        return "", "FALSE", {}
    ors, params = [], {}
    for i, w in enumerate(acc_words):
        ors.append(f"account ILIKE :acc{i}")
        params[f"acc{i}"] = f"%{w}%"
    joined = " OR ".join(ors)
    return " OR " + joined, "(" + joined + ")", params


def semantic_filter(project, time_range, source: str | None = None,
                    links: LinkScope | None = None) -> tuple[str, dict[str, Any]]:
    """Фильтр для ANN-кандидатов: проект/окно/источник/«не разговор с Верой» —
    всё, кроме текстового условия (его отсутствие и есть смысл ANN)."""
    if project is not None:
        return project_clause(project, time_range, source, links)
    src_sql, params = source_clause(source)
    link_sql, link_params = links_clause(links)
    src_sql, params = src_sql + link_sql, {**params, **link_params}
    if time_range:
        params = {**params, "t_start": time_range[0], "t_end": time_range[1]}
        return (f"occurred_at >= :t_start AND occurred_at < :t_end"
                f"{NOT_A_WORLD_EVENT}{src_sql}", params)
    return f"TRUE{NOT_A_WORLD_EVENT}{src_sql}", params
