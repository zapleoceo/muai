"""Vera 3.0 search service — HTTP-слой.

Здесь только маршруты и разбор запроса. Всё остальное разъехалось по
соседям, потому что в одном файле на 530 строк жили сразу: роутинг, своя
копия проверки секрета, семь pydantic-моделей, ШЕСТЬ почти одинаковых
SELECT'ов, алгоритм скоринга, кэш самоописания со своим SQL и сборка
промпта с русским текстом внутри. Конвенция проекта — «один файл = одна
ответственность, потолок ~200 строк».

    models.py          формы запроса/ответа
    lang.py            служебные слова ru/uk/en/id для tsquery
    pipeline.py        вопрос → кандидаты → скоринг (общий с агентом)
    retrieval.py       выборка кандидатов (один запрос вместо шести копий)
    scoring.py         ранжирование
    self_context.py    «кто я и что подключено» + кэш
    synthesis.py       промпт и получение ответа (агент или прямой синтез)
    reports.py         точная SQL-агрегация вместо пересказа top-N
    search_service.py  ветки /search: отчёт, точные id, цитата, время, эмбеддинг
"""
from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from vera_shared.auth import internal_secret_ok
from vera_shared.db.engine import close_engine, init_engine

from brain_search.models import AnswerResponse, SearchQuery
from brain_search.search_service import InvalidSearchFilters, run_search

log = logging.getLogger(__name__)


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


@app.post("/search", response_model=AnswerResponse)
async def search(
    query: SearchQuery,
    x_internal_secret: str | None = Header(default=None),
) -> AnswerResponse:
    """Гибридный поиск + LLM-синтез ответа."""
    check_internal_secret(x_internal_secret)
    try:
        return await run_search(query)
    except InvalidSearchFilters as e:
        raise HTTPException(422, f"filters: {e}") from e
