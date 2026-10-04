"""Execute exact-ID SQL against synthetic records, preserving every scope."""
from __future__ import annotations

from datetime import datetime

import pytest
from brain_search.query_parse import ProjectScope
from brain_search.retrieval import LinkScope, _exact_rows
from vera_shared.db.models import EventRow
from vera_shared.db.models_graph import EntityRow
from vera_shared.db.models_links import EventEntityRow
from vera_shared.links.filters import EventFilter


@pytest.mark.parametrize(("overrides", "scope_ok", "expected"), [
    ({}, True, [900101]),
    ({"triage_status": "hidden"}, True, []),
    ({"account": "other-account"}, True, []),
    ({"account": "test-account' OR 1=1 --"}, True, []),
    ({"source": "slack"}, True, []),
    ({"source": "vera_chat"}, True, []),
    ({"nature": "conversation_with_me"}, True, []),
    ({"nature": "my_intent"}, True, []),
    ({"project": "other-project"}, True, []),
    ({"occurred_at": datetime(2026, 10, 3, 12)}, True, []),
    ({}, False, []),
])
@pytest.mark.asyncio
async def test_exact_lookup_keeps_visibility_and_structured_scope(
    sqlite_db, overrides, scope_ok, expected,
):
    event = {"id": 900101, "source": "gmail", "source_event_id": "synthetic-900101",
             "account": "test-account", "project": "synthetic-project",
             "occurred_at": datetime(2026, 10, 4, 12), "content_text": "Synthetic notice",
             "triage_status": "done"}
    async with sqlite_db() as session:
        session.add(EventRow(**(event | overrides)))
        session.add(EntityRow(id=7001, type="person", name="Synthetic Person"))
        await session.flush()
        session.add(EventEntityRow(event_id=900101, entity_id=7001, role="author",
                                   source_of_link="test", scope_ok=scope_ok))

    start, end = datetime(2026, 10, 4), datetime(2026, 10, 5)
    scope = LinkScope(EventFilter(account="test-account", project="synthetic-project",
                                 source="gmail", kind="email", start=start, end=end,
                                 participant_ids=(7001,), author_ids=(7001,)))
    async with sqlite_db() as session:
        rows = await _exact_rows(session, [900101, 900102], "gmail", scope,
                                 time_range=(start, end),
                                 project=ProjectScope("synthetic-project"))
    # 900102 was never inserted: it must not substitute for an inaccessible ID.
    assert [row.id for row in rows] == expected


@pytest.mark.asyncio
async def test_exact_lookup_obeys_explicit_window_without_link_filters(sqlite_db):
    async with sqlite_db() as session:
        session.add(EventRow(id=900101, source="gmail", source_event_id="synthetic",
                             occurred_at=datetime(2026, 10, 4, 12), content_text="Notice"))
    async with sqlite_db() as session:
        rows = await _exact_rows(session, [900101], None, None,
                                 time_range=(datetime(2026, 10, 5), datetime(2026, 10, 6)))
    assert rows == []
