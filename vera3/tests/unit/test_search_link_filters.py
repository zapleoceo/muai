"""brain-search: фильтр по людям сужает кандидатов ДО ранжирования (WHERE во всех режимах) и
неверный фильтр — 422, а не молча пустой ответ. Данные синтетические."""
from __future__ import annotations

import pytest
from brain_search import app as search_app
from brain_search.models import SearchQuery
from brain_search.query_parse import ProjectScope
from brain_search.retrieval_filters import (
    LinkScope,
    links_clause,
    project_clause,
    semantic_filter,
)
from brain_search.search_service import InvalidSearchFilters, link_scope
from fastapi import HTTPException
from vera_shared.links.filters import EventFilter

SCOPE = LinkScope(EventFilter(participant_ids=(7,), kind="call"), None)


def test_no_scope_adds_nothing():
    assert links_clause(None) == ("", {})
    assert links_clause(LinkScope(EventFilter(), None)) == ("", {})


def test_scope_becomes_exists_conditions_on_events():
    sql, params = links_clause(SCOPE)
    assert sql.startswith(" AND EXISTS (SELECT 1 FROM event_entities l WHERE l.event_id = events.id")
    assert "events.source IN (:s0)" in sql and params["p0"] == 7 and params["s0"] == "voice"


def test_every_candidate_mode_carries_the_scope():
    project = ProjectScope(name="itstep", triggers=("itstep",))
    assert "event_entities" in project_clause(project, None, None, SCOPE)[0]
    assert "event_entities" in semantic_filter(project, None, None, SCOPE)[0]
    assert "event_entities" in semantic_filter(None, None, None, SCOPE)[0]
    assert "event_entities" not in semantic_filter(None, None, None, None)[0]


@pytest.mark.asyncio
async def test_bad_filters_are_rejected_before_retrieval():
    for bad in ({"kind": "video"}, {"unknown": 1}, {"participant_ids": list(range(11))}):
        with pytest.raises(InvalidSearchFilters):
            await link_scope(SearchQuery(q="x", filters=bad))
    assert await link_scope(SearchQuery(q="x")) is None
    scope = await link_scope(SearchQuery(q="x", filters={"participant_ids": [3], "kind": "email"}))
    assert scope.flt.participant_ids == (3,) and scope.flt.kind == "email"


@pytest.mark.asyncio
async def test_bad_filters_are_422_at_the_route(monkeypatch):
    monkeypatch.setenv("INTERNAL_SECRET", "synthetic-test-secret")
    for bad in ({"kind": "video"}, {"unknown": 1}, {"participant_ids": list(range(11))}):
        with pytest.raises(HTTPException) as e:
            await search_app.search(SearchQuery(q="x", filters=bad), "synthetic-test-secret")
        assert e.value.status_code == 422
        assert str(e.value.detail).startswith("filters: ")
