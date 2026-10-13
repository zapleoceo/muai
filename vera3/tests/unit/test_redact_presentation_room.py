"""redact_view в выдаче комнаты агентов: маскируется текст, идентификаторы и токены целы."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from vera_mcp import room_question_tools as rq
from vera_mcp import room_tools as r
from vera_shared.db.engine import get_session
from vera_shared.redact_view import MASK

GH = "ghp_" + "CANARY" * 4
UUID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"


def ctx(client: str):
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": client})))


CLAUDE, CODEX = ctx("claude"), ctx("codex")
LOOK_ID = "msg-" + GH


@pytest.fixture
def _rooms(monkeypatch):
    monkeypatch.setenv("ROOM_NAMES", "main")


@pytest.mark.asyncio
async def test_room_messages_masked_ids_byte_identical_and_dedupe_on_raw(sqlite_db, _rooms):
    body = f"deploy token {GH} uuid {UUID}"
    first = await r.room_post(body, CLAUDE, message_id=LOOK_ID, task_id=LOOK_ID)
    again = await r.room_post(body, CLAUDE, message_id=LOOK_ID, task_id=LOOK_ID)
    assert again["deduped"] is True and again["message"]["id"] == first["message"]["id"]
    for msgs in ((await r.room_inbox(CODEX))["messages"],
                 (await r.room_history())["messages"]):
        m = msgs[0]
        assert "CANARY" not in m["body"] and UUID in m["body"] and MASK in m["body"]
        assert (m["message_id"], m["task_id"], m["from"]) == (LOOK_ID, LOOK_ID, "claude")
    async with get_session() as s:
        from sqlalchemy import select
        from vera_shared.db.models_room import RoomMessageRow
        stored = (await s.execute(select(RoomMessageRow.body))).scalar_one()
    assert stored == body


@pytest.mark.asyncio
async def test_room_tasks_and_task_history_masked_tokens_intact(sqlite_db, _rooms):
    task = (await r.room_task_claim(LOOK_ID, CLAUDE, title=f"fix token {GH}"))["task"]
    await r.room_task_update(LOOK_ID, task["fencing_token"], CLAUDE, note=f"n token {GH}",
                             next_action=f"password: CANARY-pw-2 {UUID}")
    await r.room_task_progress(LOOK_ID, task["fencing_token"], CLAUDE, result=f"done token {GH}")
    listed = (await r.room_tasks())["tasks"][0]
    assert "CANARY" not in str({k: listed[k] for k in
                                ("title", "note", "next_action", "last_progress_text")})
    assert UUID in listed["next_action"]
    assert (listed["task_id"], listed["fencing_token"], listed["lease_holder"], listed["status"]) == (
        LOOK_ID, task["fencing_token"], "claude", "in_progress")
    events = (await r.room_task_history(LOOK_ID))["events"]
    assert events and "CANARY" not in str([e["text"] for e in events])
    assert all(e["task_id"] == LOOK_ID and e["fencing_token"] in (None, task["fencing_token"])
               for e in events)


@pytest.mark.asyncio
async def test_room_questions_masked(sqlite_db, _rooms):
    task = (await r.room_task_claim("t1", CLAUDE))["task"]
    await rq.room_task_ask("t1", task["fencing_token"], CLAUDE, f"ключ token {GH}?")
    out = (await rq.room_task_questions("t1"))["questions"][0]
    assert "CANARY" not in out["question"] and out["task_id"] == "t1"
