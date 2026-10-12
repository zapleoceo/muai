"""Хуки redact_view на поверхностях выдачи: MCP, gateway, brain-search. Синтетика CANARY."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from brain_search import agent_tools, synthesis
from brain_search.agent_tools import SearchEventsArgs
from brain_search.models import SearchQuery, SearchResult
from brain_search.rows import Candidate
from gateway import query as gw
from vera_mcp import read_tools as r
from vera_mcp import link_tools
from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.redact_view import MASK
from vera_shared.timeutil import utc_naive_now

GH = "ghp_" + "CANARY" * 4
AWS = "AKIA" + "CANARY0000000000"
UUID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
TX = "https://my.sepay.vn/transactions/details/85261396"
LEAKY = (f"SIN-4905 Код: {UUID} tx 4930118822771 {TX} sha {SHA256}\n"
         f"token {GH} {AWS}\npassword: CANARY-pw-1\n"
         "https://x.test/cb?token=CANARYtok123456&page=2\n"
         "ftp://user:CANARYpw@h.test/p\nhttps://s3.x.test/f?id=7&X-Amz-Signature=CANARYsig99aa")
INTACT = (UUID, SHA256, "4930118822771", TX, "page=2")


def assert_clean(blob: str) -> None:
    assert "CANARY" not in blob, blob
    for keep in INTACT:
        assert keep in blob, keep


def cand(event_id: int = 7, text: str = LEAKY) -> Candidate:
    return Candidate(id=event_id, source="gmail", source_event_id=f"s{event_id}",
                     occurred_at=datetime(2026, 10, 9, 9, tzinfo=timezone.utc),
                     content_text=text, importance=50, embedding=None, rank=1.0, account="a")


# ─── MCP ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_search_redacts_preview():
    data = {"results": [{"event_id": 1, "content_preview": LEAKY,
                         "metadata": {"accessToken": "CANARYopaque123", "id": UUID}},
                        {"event_id": 2}]}
    with patch.object(r, "search_brain", AsyncMock(return_value=data)):
        out = await r.search("q")
    assert_clean(out["results"][0]["content_preview"])
    assert out["results"][0]["metadata"] == {"accessToken": MASK, "id": UUID}
    assert out["results"][1] == {"event_id": 2}


@pytest.mark.asyncio
async def test_mcp_recent_and_get_event_redact_only_secret_leaves(sqlite_db):
    meta = {"chat_title": "Team", "uuid": UUID, "headers": {"Authorization": "Bearer CANARYbearer1"},
            "note": f"k {GH}", "msg_id": 42}
    async with get_session() as s:
        s.add(EventRow(id=1, source="telegram", source_event_id="tg:1:2", account="a",
                       content_text=LEAKY, transcript_text=f"say {GH}", occurred_at=utc_naive_now(),
                       triage_status="done", metadata_=meta, content_extra={"password": "CANARY-pw-9"},
                       triage_metadata={"why": f"found {AWS}"}))
    got = await r.get_event(1)
    assert got["source_event_id"] == "tg:1:2" and got["id"] == 1
    assert_clean(got["content_text"])
    assert "CANARY" not in got["transcript_text"] and "CANARY" not in str(got["triage"])
    assert got["content_extra"] == {"password": MASK}
    assert got["metadata"] == {**meta, "headers": {"Authorization": MASK}, "note": f"k {MASK}"}
    recent = await r.recent_events(hours=1)
    assert recent["count"] == 1 and "CANARY" not in str(recent["events"])


@pytest.mark.asyncio
async def test_mcp_remaining_read_views_redact_at_return_boundary(monkeypatch):
    payload = {"entity_id": 7, "name": "Example", "attributes": {"api_key": "CANARYopaque"},
               "members": [{"id": 8, "note": LEAKY}], "mentions": []}
    monkeypatch.setattr(r, "entity_context_payload", AsyncMock(return_value=payload))
    context = await r.entity_context(entity_id=7)
    assert context["attributes"]["api_key"] == MASK
    assert context["members"][0]["id"] == 8
    assert_clean(context["members"][0]["note"])
    assert payload["attributes"]["api_key"] == "CANARYopaque"  # source is untouched

    events = [{"id": 9, "content_preview": LEAKY, "metadata": {"password": "CANARYpw"}}]
    monkeypatch.setattr(r, "entity_events", AsyncMock(return_value=events))
    out = await r.timeline(7, start="2026-01-01", roles=["author"])
    assert out["events"][0]["id"] == 9
    assert out["events"][0]["metadata"]["password"] == MASK
    assert_clean(out["events"][0]["content_preview"])

    monkeypatch.setattr(r.audit, "list_entries", AsyncMock(return_value=[
        {"id": 10, "tool": "hide_event", "args": {"client_secret": "CANARYopaque"},
         "reason": LEAKY}]))
    audit = await r.audit_log()
    assert audit["entries"][0]["id"] == 10
    assert audit["entries"][0]["args"]["client_secret"] == MASK
    assert_clean(audit["entries"][0]["reason"])


@pytest.mark.asyncio
async def test_mcp_link_views_redact_without_mutating_source(monkeypatch):
    people = {"event_id": 9, "participants": [{"entity_id": 7, "token": GH}],
              "unresolved_speakers": [{"label": UUID}]}
    monkeypatch.setattr(link_tools.links_read, "event_participants",
                        AsyncMock(return_value=people))
    out = await link_tools.event_participants(9)
    assert out["participants"][0]["token"] == MASK
    assert out["unresolved_speakers"][0]["label"] == UUID
    assert people["participants"][0]["token"] == GH

    grouped = {"count": 1, "events": [{"id": 9, "content_preview": LEAKY}],
               "by_kind": {"message": 1}}
    monkeypatch.setattr(link_tools.links_read, "co_occurrence", AsyncMock(return_value=grouped))
    out = await link_tools.co_occurrence(7, 8)
    assert out["by_kind"] == {"message": 1}
    assert_clean(out["events"][0]["content_preview"])


@pytest.mark.asyncio
async def test_sql_query_redacts_values_by_column_even_with_duplicate_names(monkeypatch):
    raw = {"columns": ["access_token", "access_token", "note", "id"],
           "rows": [["CANARYone", "CANARYtwo", LEAKY, 9]],
           "row_count": 1, "truncated": False}
    monkeypatch.setattr(r, "run_readonly", AsyncMock(return_value=raw))
    out = await r.sql_query("SELECT 1")
    assert out["columns"] == raw["columns"]
    assert out["rows"][0][:2] == [MASK, MASK]
    assert_clean(out["rows"][0][2])
    assert out["rows"][0][3] == 9


# ─── gateway ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gateway_search_redacts_answer_results_trace():
    data = {"answer": LEAKY, "results": [{"event_id": 1, "content_preview": LEAKY}],
            "agent_trace": [{"step": 1, "observation": {"preview": LEAKY}, "password": "CANARY-pw-1"}]}
    with patch.object(gw, "search_brain", AsyncMock(return_value=data)):
        out = await gw.search_proxy(gw.SearchProxyRequest(q="x"),
                                    x_internal_secret="test-internal-secret")
    assert_clean(out["answer"])
    assert_clean(out["results"][0]["content_preview"])
    assert "CANARY" not in str(out["agent_trace"]) and UUID in str(out["agent_trace"])


@pytest.mark.asyncio
async def test_gateway_recent_events_redacted():
    ev = [{"id": 1, "content_preview": LEAKY}]
    with patch.object(gw, "query_recent_events", AsyncMock(return_value=(ev, False))):
        out = await gw.recent_events(hours=1, source=None, x_internal_secret="test-internal-secret")
    assert_clean(out["events"][0]["content_preview"])


# ─── brain-search ───────────────────────────────────────────────────────────


def _result(event_id: int = 7) -> SearchResult:
    return SearchResult(score=1.0, event_id=event_id, source="gmail", source_url=None,
                        occurred_at="2026-10-09 09:00:00", content_preview="", importance=50)


def test_build_context_redacts_before_llm():
    assert_clean(synthesis.build_context([_result()], {7: cand()}))


async def _run_answer(monkeypatch, *, reply: str, agent: bool = False):
    prompts: list[str] = []

    async def fake_chat(*, messages, **_kw):
        prompts.append(messages[0]["content"])
        return reply, {"provider": "t", "cost_usd": 0.0}

    trace = SimpleNamespace(answer=reply, provider_last="t", cost_usd=0.0, final_step=1,
                            steps=[{"step": 1, "observation": LEAKY}])
    monkeypatch.setattr(synthesis, "chat_async", fake_chat)
    monkeypatch.setattr(synthesis, "self_context", AsyncMock(return_value="cfg"))
    monkeypatch.setattr(synthesis, "run_agent", AsyncMock(return_value=trace))
    query = SearchQuery(q="SIN-4905", use_agent=agent)
    return await synthesis.answer(query, [cand()], None), prompts


@pytest.mark.asyncio
async def test_answer_prompt_and_response_clean_and_exact_identifier_still_hits(monkeypatch):
    resp, prompts = await _run_answer(monkeypatch, reply=LEAKY)
    assert_clean(prompts[0])
    assert "[event:7]" in prompts[0] and "SIN-4905" in prompts[0]
    assert_clean(resp.answer)
    assert_clean(resp.results[0].content_preview)
    assert "нет событий" not in prompts[0].lower()


@pytest.mark.asyncio
async def test_identifier_hit_is_computed_on_raw_text(monkeypatch):
    raw = f"Код: {UUID} password: CANARY-pw-1"
    monkeypatch.setattr(synthesis, "self_context", AsyncMock(return_value="cfg"))
    seen: dict[str, str] = {}

    def spy(identifiers, texts):
        seen.update({str(k): v for k, v in texts.items()})
        return {}

    monkeypatch.setattr(synthesis, "identifier_hits", spy)
    monkeypatch.setattr(synthesis, "chat_async",
                        AsyncMock(return_value=("ok", {"provider": "t", "cost_usd": 0.0})))
    await synthesis.answer(SearchQuery(q=UUID, use_agent=False), [cand(text=raw)], None)
    assert seen["7"] == raw


@pytest.mark.asyncio
async def test_agent_branch_redacts_answer_and_trace(monkeypatch):
    resp, _ = await _run_answer(monkeypatch, reply=LEAKY, agent=True)
    assert_clean(resp.answer)
    assert "CANARY" not in str(resp.agent_trace) and UUID in str(resp.agent_trace)


@pytest.mark.asyncio
async def test_agent_tool_excerpt_redacted(monkeypatch):
    monkeypatch.setattr(agent_tools, "search_ranked",
                        AsyncMock(return_value=(None, [(1.0, cand())])))
    res = await agent_tools._exec_search_events(SearchEventsArgs(q="SIN-4905"))
    assert_clean(res["events"][0]["preview"])


@pytest.mark.asyncio
async def test_agent_tool_results_redacted_at_boundary(monkeypatch):
    tool = SimpleNamespace(name="tg", invoker="http_telegram")
    monkeypatch.setattr(agent_tools, "_dispatch",
                        AsyncMock(return_value={"messages": [{"text": LEAKY}], "body": GH}))
    res = await agent_tools.execute_tool(tool, {})
    assert "CANARY" not in str(res) and UUID in str(res)
    monkeypatch.setattr(agent_tools, "_dispatch", AsyncMock(side_effect=RuntimeError(f"boom {GH}")))
    assert "CANARY" not in str(await agent_tools.execute_tool(tool, {}))
