"""Все инструменты комнаты, возвращающие задачу или сообщение, маскируют свободный текст;
id, fencing_token, статусы и ссылки (kind/ref) остаются байт в байт. Синтетика CANARY."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from vera_mcp import room_tools as r
from vera_mcp.room_redact import events_view
from vera_mcp.room_task_tools import room_task_wait
from vera_shared.redact_view import MASK

GH = "ghp_" + "CANARY" * 4
UUID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
TID = "task-" + GH


def ctx(client: str):
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": client})))


CLAUDE = ctx("claude")
REFS = [{"kind": "url", "ref": "https://x.test/a?id=1", "excerpt": f"see token {GH} {UUID}"}]


@pytest.fixture(autouse=True)
def _rooms(monkeypatch):
    monkeypatch.setenv("ROOM_NAMES", "main")


def masked(task: dict) -> None:
    blob = str({k: task[k] for k in ("title", "note", "next_action", "last_progress_text", "refs")})
    assert "CANARY" not in blob
    assert task["task_id"] == TID and task["refs"][0]["ref"] == "https://x.test/a?id=1"


@pytest.mark.asyncio
async def test_every_task_tool_masks_what_it_returns(sqlite_db):
    opened = await r.room_task_open(TID, CLAUDE, title=f"t token {GH}",
                                    next_action=f"password: CANARY-pw-1 {UUID}", refs=REFS)
    masked(opened["task"])
    claimed = (await r.room_task_claim(TID, CLAUDE))["task"]
    masked(claimed)
    tok = claimed["fencing_token"]
    assert isinstance(tok, int)
    for res in (
        await r.room_task_update(TID, tok, CLAUDE, note=f"n token {GH}", status="in_progress"),
        await r.room_task_progress(TID, tok, CLAUDE, result=f"did token {GH}"),
        await r.room_task_state(TID, tok, CLAUDE, state="blocked", reason=f"why token {GH}"),
        await r.room_task_state(TID, tok, CLAUDE, state="unblocked", reason="ok"),
        await room_task_wait(TID, tok, CLAUDE, until_seconds=60, reason=f"w token {GH}"),
    ):
        masked(res["task"])
        assert res["task"]["fencing_token"] == tok
    released = await r.room_task_release(TID, tok, CLAUDE, status="open", note=f"x token {GH}")
    masked(released["task"])
    assert released["task"]["fencing_token"] == tok
    nxt = (await r.room_task_next(CLAUDE))["task"]
    if nxt:
        masked(nxt)


@pytest.mark.asyncio
async def test_room_post_returns_masked_message_but_stores_raw(sqlite_db):
    body = f"token {GH} {UUID}"
    res = await r.room_post(body, CLAUDE, message_id="m-" + GH)
    assert "CANARY" not in res["message"]["body"] and UUID in res["message"]["body"]
    assert res["message"]["message_id"] == "m-" + GH
    again = await r.room_post(body, CLAUDE, message_id="m-" + GH)
    assert again["deduped"] is True


@pytest.mark.asyncio
async def test_task_history_masks_refs_excerpt_in_event_data(sqlite_db):
    task = (await r.room_task_claim(TID, CLAUDE))["task"]
    await r.room_task_update(TID, task["fencing_token"], CLAUDE, refs=REFS,
                             next_action="password: CANARY-pw-2")
    events = (await r.room_task_history(TID))["events"]
    assert "CANARY" not in str(events).replace(TID, "")
    assert all(e["task_id"] == TID for e in events)


def test_event_data_before_after_and_refs_masked_ids_intact():
    ev = {"id": 3, "task_id": TID, "fencing_token": 9, "text": f"t {GH}", "data": {
        "qid": 4, "before": {"next_action": f"a token {GH}", "responsible": "claude"},
        "after": {"next_action": "password: CANARY-pw-3", "responsible": "codex"},
        "refs": REFS}}
    out = events_view([ev])[0]
    assert "CANARY" not in str(out).replace(TID, "") and MASK in str(out)
    assert (out["id"], out["task_id"], out["fencing_token"], out["data"]["qid"]) == (3, TID, 9, 4)
    assert out["data"]["refs"][0]["ref"] == REFS[0]["ref"] and out["data"]["before"]["responsible"] == "claude"
    assert UUID in out["data"]["refs"][0]["excerpt"]
