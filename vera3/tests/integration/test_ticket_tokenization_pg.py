"""Ticket phrase matching against real Postgres tokenization (literals only, no tables)."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.skipif(not os.environ.get("RUN_INTEGRATION_TESTS"),
                               reason="Requires an existing local PostgreSQL test database")

_MATCH = text("""
    SELECT to_tsvector(:cfg, :doc) @@ phraseto_tsquery(:cfg, :ticket) AS fts,
           (:doc ~* ('(^|[^[:alnum:]])' || :ticket || '($|[^[:alnum:]])')) AS bounded
""")


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", ["russian", "indonesian"])
@pytest.mark.parametrize(("doc", "fts", "bounded"), [
    ("письмо SIN-4905 тест", True, True),
    ("[Jira] (SIN-4905) «Проверить»", True, True),
    ("see SIN-4905.", True, True),
    ("XSIN-4905", False, False),
    ("SIN-49055", False, False),
])
async def test_phraseto_matches_ticket_as_indexed(cfg: str, doc: str, fts: bool, bounded: bool):
    url = make_url(os.environ["TEST_DATABASE_URL"])
    assert url.host in {"localhost", "127.0.0.1", "::1"}
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(_MATCH, {"cfg": cfg, "doc": doc,
                                               "ticket": "SIN-4905"})).one()
    finally:
        await engine.dispose()
    assert row.bounded is bounded
    assert row.fts is fts
