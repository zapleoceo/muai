"""Разбор запроса и выбор ветки выборки в brain-search.

В `search()` было шесть почти одинаковых SELECT'ов, различавшихся только
WHERE и LIMIT, и понять, в какой ты ветке, можно было только сравнив их
глазами. Теперь ветка — это `Candidates.mode`, и её можно проверить.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from brain_search import app as bs
from brain_search.retrieval import account_clause, project_clause

# ─── разбор запроса ─────────────────────────────────────────────────────────


def test_stopwords_dropped_and_prefix_query_built():
    ts, acc = bs._ts_query("что было по проекту Itstep")
    assert "что:*" not in ts and "по:*" not in ts     # стопслова выкинуты
    assert "Itstep:*" in ts
    assert "проекту:*" in ts
    assert acc == ["itstep"], "в account ищем только имена собственные (нормализованы)"


def test_single_letter_words_dropped():
    ts, _ = bs._ts_query("а б Иван")
    assert ts == "Иван:*"


def test_empty_query_yields_no_tsquery():
    assert bs._ts_query("и в на")[0] == ""


@pytest.mark.asyncio
async def test_embed_failure_is_not_fatal(monkeypatch):
    """Брокер лежит — поиск обязан продолжить на одном FTS."""
    async def boom(_texts):
        raise bs.LLMCallFailed("брокер лёг")

    monkeypatch.setattr(bs, "embed", boom)
    assert await bs._embed_query("вопрос") is None


@pytest.mark.asyncio
async def test_embed_timeout_is_not_fatal(monkeypatch):
    import asyncio

    async def hang(_texts):
        await asyncio.sleep(3600)

    monkeypatch.setattr(bs, "embed", hang)
    monkeypatch.setattr(bs, "EMBED_TIMEOUT_S", 0.02)
    assert await bs._embed_query("вопрос") is None


@pytest.mark.asyncio
async def test_embed_returns_first_vector(monkeypatch):
    monkeypatch.setattr(bs, "embed", AsyncMock(return_value=[[0.1, 0.2]]))
    assert await bs._embed_query("вопрос") == [0.1, 0.2]


# ─── сборка WHERE ───────────────────────────────────────────────────────────


def test_project_clause_matches_column_or_registry():
    """ПЕРВИЧНЫЙ сигнал — колонка project (её ставит триаж по содержимому),
    реестр ящиков и чатов — fallback для неклассифицированных событий."""
    project = SimpleNamespace(name="itstep",
                              account_like=["itstep.org"],
                              chats=["ITSTEP HQ"])
    where, params = project_clause(project, None)

    assert "project = :pname" in where
    assert "account ILIKE :pacc0" in where
    assert "metadata->>'chat_title' = ANY(:pchats)" in where
    # разговоры с Верой — не события мира
    assert "conversation_with_me" in where and "source <> 'vera_chat'" in where
    assert params["pname"] == "itstep"
    assert params["pacc0"] == "%itstep.org%"


def test_project_clause_adds_time_window():
    from datetime import datetime
    project = SimpleNamespace(name="p", account_like=[], chats=[])
    rng = (datetime(2026, 9, 1), datetime(2026, 9, 2))
    where, params = project_clause(project, rng)
    assert "occurred_at >= :t_start" in where
    assert params["t_start"] == rng[0] and params["t_end"] == rng[1]


def test_account_clause_empty_is_false_not_broken_sql():
    """Без имён собственных выражение должно быть валидным FALSE, иначе
    ORDER BY acc_match развалит запрос."""
    where, match, params = account_clause([])
    assert where == "" and match == "FALSE" and params == {}


def test_account_clause_builds_or_chain():
    where, match, params = account_clause(["Itstep", "Veranda"])
    assert where.startswith(" OR ")
    assert match == "(account ILIKE :acc0 OR account ILIKE :acc1)"
    assert params == {"acc0": "%Itstep%", "acc1": "%Veranda%"}
