"""Quoted FTS on existing local/test PostgreSQL; only temporary synthetic rows."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

pytestmark = pytest.mark.skipif(not os.environ.get("RUN_INTEGRATION_TESTS"),
                               reason="Requires an existing local PostgreSQL test database")

SHORT = '"amber otter lantern"'
LONG = ('Find the exact phrase "amber otter lantern" in Synthetic Cedar chat '
        'on 2026-10-04. Show the message and correct author attribution; '
        'distinguish participant speech from the owner and nearby messages.')
DISTRACTOR = ('Synthetic Cedar chat: show message author attribution, distinguish '
              'participant speech from owner and nearby messages. Search exact phrase.')


@pytest_asyncio.fixture
async def quote_pg_events(monkeypatch):
    url = make_url(os.environ["TEST_DATABASE_URL"])
    assert url.host in {"localhost", "127.0.0.1", "::1"}
    assert url.database and "test" in url.database.lower()
    assert url.drivername == "postgresql+asyncpg"
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("""
                CREATE TEMP TABLE events (
                    id bigint PRIMARY KEY, source text, source_event_id text,
                    occurred_at timestamp, content_text text, transcript_text text,
                    importance integer, account text, nature text,
                    triage_status text, project text, metadata jsonb
                ) ON COMMIT DROP
            """))
            base = {"source": "telegram", "account": "test-account", "status": "done",
                    "at": datetime(2026, 10, 4, 12), "body": "amber otter lantern"}
            rows = [{"id": 900101}]
            rows += [{"id": 900200 + i, "body": DISTRACTOR} for i in range(12)]
            rows += [{"id": 900301, "account": "other-account"},
                     {"id": 900302, "source": "gmail"},
                     {"id": 900303, "status": "hidden"},
                     {"id": 900304, "at": datetime(2026, 10, 5, 12)}]
            await conn.execute(text("""
                INSERT INTO events
                    (id, source, source_event_id, occurred_at, content_text,
                     importance, account, triage_status, metadata)
                VALUES (:id, :source, :external_id, :at, :body,
                        50, :account, :status, '{}')
            """), [base | row | {"external_id": str(row["id"])} for row in rows])

            @asynccontextmanager
            async def session():
                async with AsyncSession(bind=conn) as db:
                    yield db

            async def no_embed(_question):
                return None

            from brain_search import pipeline, retrieval
            monkeypatch.setattr(retrieval, "get_session", session)
            monkeypatch.setattr(pipeline, "embed_query", no_embed)
            yield
            await conn.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("limit", [3, 10])
@pytest.mark.asyncio
async def test_context_preserves_quote_with_source_account_day_scope(quote_pg_events, limit):
    from brain_search.pipeline import query_terms, search_ranked
    from brain_search.query_parse import parse_time_range, resolve_project
    from brain_search.retrieval import LinkScope
    from vera_shared.links.filters import EventFilter

    scope = LinkScope(EventFilter(source="telegram", account="test-account",
                                 start=datetime(2026, 10, 3, 17),
                                 end=datetime(2026, 10, 4, 17)))
    results = {}
    diagnostics = {}
    for label, question in (("short", SHORT), ("long", LONG)):
        found, ranked = await search_ranked(question, limit=limit, source="telegram",
                                           links=scope, time_range=parse_time_range(question),
                                           project=resolve_project(question))
        results[label] = [row.id for _, row in ranked]
        diagnostics[label] = {"mode": found.mode, "terms": query_terms(question),
                              "candidates": [row.id for row in found.rows]}
    assert results["short"] == [900101], diagnostics
    assert results["long"] == results["short"], diagnostics
