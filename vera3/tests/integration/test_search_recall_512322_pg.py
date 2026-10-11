"""Run the production recall SQL against temporary synthetic PostgreSQL rows."""
from __future__ import annotations

import os
from datetime import datetime

import pytest
from brain_search.query_parse import ProjectScope
from brain_search.retrieval import _exact_rows, _primary
from brain_search.rows import Candidate
from brain_search.title_rows import fetch_title_rows
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.skipif(not os.environ.get("RUN_INTEGRATION_TESTS"),
                               reason="Requires a local PostgreSQL test database")


@pytest.mark.asyncio
async def test_date_and_exact_id_recall_with_title_and_visibility():
    url = make_url(os.environ["TEST_DATABASE_URL"])
    assert url.drivername == "postgresql+asyncpg"
    assert url.host in {"localhost", "127.0.0.1", "::1"}
    assert url.database and "test" in url.database.lower()
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("""
                CREATE TEMP TABLE events (
                    id bigint PRIMARY KEY, source text, source_event_id text,
                    occurred_at timestamp, content_text text, transcript_text text,
                    importance integer, account text, nature text, triage_status text,
                    project text, metadata jsonb
                ) ON COMMIT DROP
            """))
            await conn.execute(text("""
                INSERT INTO events (id, source, source_event_id, occurred_at,
                                    content_text, importance, account, triage_status,
                                    project, metadata)
                VALUES (:id, 'gmail', :source_id, :at, :body, 50,
                        'synthetic-account', :status, :project, :metadata)
            """), [
                {"id": 512322, "source_id": "synthetic-512322",
                 "at": datetime(2026, 10, 4, 12),
                 "body": "Author: Test\nFrom: test@example.test\nTo: owner@example.test\nSubject: Synthetic Sapphire Notice\n---\nText body",
                 "status": "done", "project": "synthetic-project", "metadata": "{}"},
                {"id": 900100, "source_id": "synthetic-noise",
                 "at": datetime(2026, 10, 4, 13),
                 "body": "Subject: Synthetic unrelated notice\n---\nNoise",
                 "status": "done", "project": "synthetic-project", "metadata": "{}"},
                {"id": 900101, "source_id": "synthetic-hidden",
                 "at": datetime(2026, 10, 4, 14),
                 "body": "Subject: Synthetic Sapphire Notice\n---\nHidden",
                 "status": "hidden", "project": "synthetic-project",
                 "metadata": "{\"subject\":\"Synthetic Sapphire Notice\"}"},
                {"id": 900102, "source_id": "synthetic-other-project",
                 "at": datetime(2026, 10, 4, 15),
                 "body": "Body without a subject header",
                 "status": "done", "project": "other-project",
                 "metadata": "{\"subject\":\"Synthetic Sapphire Notice\"}"},
            ])
            window = (datetime(2026, 10, 3, 17), datetime(2026, 10, 4, 17))
            titled = await _primary(conn, ts_query="Sapphire:*", acc_words=[],
                                    time_range=window, project=None, q_vec=None,
                                    with_vec=False, limit=15)
            assert Candidate.of(titled.rows[0]).id == 512322
            exact_title = await fetch_title_rows(
                conn, "Synthetic Sapphire Notice", source=None, links=None,
                time_range=window, project=ProjectScope("synthetic-project"))
            assert [row.id for row in exact_title] == [512322]
            assert exact_title[0].rank == 100.0
            assert await fetch_title_rows(
                conn, "Synthetic Sapphire Notice", source="slack", links=None,
                time_range=window) == []
            dated = await _primary(conn, ts_query="", acc_words=[],
                                   time_range=window, project=None, q_vec=None,
                                   with_vec=False, limit=15)
            assert dated.mode == "time"
            assert {Candidate.of(row).id for row in dated.rows} == {512322, 900100, 900102}

            exact = await _exact_rows(conn, [512322, 900101], None, None)
            assert [Candidate.of(row).id for row in exact] == [512322]
            await conn.rollback()
    finally:
        await engine.dispose()
