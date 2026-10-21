"""Комната агентов: сообщения с идемпотентностью и курсором, задачи с арендой и fencing."""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from vera_mcp import room_tools as r
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.room import messages
from vera_shared.room.messages import MessageConflict
from vera_shared.room.tasks import StaleLease, TaskBusy, TaskNotFound

pytestmark = pytest.mark.asyncio


def ctx(client: str):
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": client})))


CLAUDE, CODEX = ctx("claude"), ctx("codex")


async def expire_lease(task_id: str, room: str = "main") -> None:
    async with get_session() as s:
        row = await s.get(RoomTaskRow, (room, task_id))
        row.lease_until = row.lease_until - timedelta(hours=10)


# ─── сообщения ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _rooms(monkeypatch):
    monkeypatch.setenv("ROOM_NAMES", "main,other")


async def inbox_bodies(c, **kw) -> list[str]:
    return [m["body"] for m in (await r.room_inbox(c, **kw))["messages"]]


async def test_post_author_is_the_token_not_an_argument(sqlite_db):
    res = await r.room_post("hello", CLAUDE, message_id="m1", status="request",
                            session="laptop")
    assert res["deduped"] is False
    msg = res["message"]
    assert (msg["from"], msg["status"], msg["session"]) == ("claude", "request", "laptop")


async def test_same_message_id_and_payload_is_idempotent(sqlite_db):
    first = await r.room_post("hello", CLAUDE, message_id="m1")
    again = await r.room_post("hello", CLAUDE, message_id="m1")
    assert again["deduped"] is True
    assert again["message"]["id"] == first["message"]["id"]
    assert len((await r.room_history())["messages"]) == 1


@pytest.mark.parametrize(("client", "body", "match"), [
    (CODEX, "hello", "already used by claude"),
    (CLAUDE, "changed body", "different payload"),
])
async def test_message_id_reuse_with_other_author_or_payload_is_refused(
        sqlite_db, client, body, match):
    await r.room_post("hello", CLAUDE, message_id="m1")
    with pytest.raises(MessageConflict, match=match):
        await r.room_post(body, client, message_id="m1")


async def test_generated_message_id_is_unique(sqlite_db):
    a = await r.room_post("a", CLAUDE)
    b = await r.room_post("b", CLAUDE)
    assert a["message"]["message_id"] != b["message"]["message_id"]


async def test_inbox_shows_others_to_me_or_all_and_needs_explicit_ack(sqlite_db):
    await r.room_post("own", CODEX)
    await r.room_post("broadcast", CLAUDE)
    await r.room_post("direct", CLAUDE, to="codex")
    await r.room_post("not for codex", CLAUDE, to="gemini")
    got = await r.room_inbox(CODEX)
    assert [m["body"] for m in got["messages"]] == ["broadcast", "direct"]
    assert got["next_cursor"] == got["messages"][-1]["id"]
    assert await inbox_bodies(CODEX) == ["broadcast", "direct"]
    acked = await r.room_ack(got["messages"][-1]["id"], CODEX)
    assert acked["cursor"] == got["messages"][-1]["id"]
    assert await inbox_bodies(CODEX) == []


async def test_ack_only_grows_and_stops_at_last_message(sqlite_db):
    last = (await r.room_post("x", CLAUDE))["message"]["id"]
    assert (await r.room_ack(last + 100, CODEX))["cursor"] == last
    assert (await r.room_ack(1, CODEX))["cursor"] == last


async def test_consumers_of_one_agent_keep_separate_cursors(sqlite_db):
    mid = (await r.room_post("job", CLAUDE))["message"]["id"]
    await r.room_ack(mid, CODEX, consumer="laptop")
    assert await inbox_bodies(CODEX, consumer="laptop") == []
    assert await inbox_bodies(CODEX, consumer="cloud") == ["job"]


async def test_inbox_since_id_rereads_without_touching_cursor(sqlite_db):
    first = (await r.room_post("one", CLAUDE))["message"]["id"]
    second = (await r.room_post("two", CLAUDE))["message"]["id"]
    await r.room_ack(second, CODEX)
    reread = await r.room_inbox(CODEX, since_id=first - 1)
    assert [m["body"] for m in reread["messages"]] == ["one", "two"]
    assert reread["cursor"] == second


async def test_rooms_are_separate(sqlite_db):
    await r.room_post("in other", CLAUDE, room="other")
    assert await inbox_bodies(CODEX) == []
    assert await inbox_bodies(CODEX, room="other") == ["in other"]


async def test_room_outside_room_names_is_refused(sqlite_db):
    with pytest.raises(ValueError, match="not open to room tokens"):
        await r.room_history(room="secret")
    with pytest.raises(ValueError, match="not open to room tokens"):
        await r.room_post("x", CLAUDE, room="secret")


async def test_history_filters_by_task_and_pages_backwards(sqlite_db):
    ids = [(await r.room_post(f"m{i}", CLAUDE, task_id="T" if i % 2 else None))
           ["message"]["id"] for i in range(4)]
    assert [m["body"] for m in (await r.room_history(task_id="T"))["messages"]] == [
        "m1", "m3"]
    assert [m["body"] for m in (await r.room_history(before_id=ids[2]))["messages"]] == [
        "m0", "m1"]


async def test_compact_inbox_full_get_and_safe_cursor(sqlite_db):
    ids = [(await r.room_post("x" * 2000, CLAUDE, message_id=f"long-{i}"))
           ["message"]["id"] for i in range(50)]
    got = await r.room_inbox(CODEX, limit=50, max_body_chars=180)
    assert len(json.dumps(got, ensure_ascii=False)) < 25_000
    assert all(m["truncated"] and len(m["body"]) == 180 for m in got["messages"])
    assert (got["cursor"], got["next_cursor"]) == (0, None)
    assert (await r.room_inbox(CODEX, max_body_chars=180))["messages"] == got["messages"]
    assert (await r.room_inbox(CODEX))["messages"][0]["body"] == "x" * 2000
    full = (await r.room_message_get(ids[0]))["message"]
    assert full["body"] == "x" * 2000 and full["truncated"] is False
    await r.room_ack(ids[-1], CODEX)
    assert (await r.room_inbox(CODEX))["messages"] == []
    assert [m["id"] for m in (await r.room_inbox(CODEX, after_id=ids[-2]))["messages"]] == [ids[-1]]
    with pytest.raises(ValueError, match="mutually exclusive"):
        await r.room_inbox(CODEX, since_id=0, after_id=0)


async def test_cross_session_delivery_and_addressed_labels(sqlite_db):
    await r.room_post("same session", CODEX, session="office")
    await r.room_post("other session", CODEX, session="orchestra")
    await r.room_post("to office", CLAUDE, to="office")
    await r.room_post("foreign label", CLAUDE, to="unclaimed")
    assert [m["body"] for m in (await r.room_inbox(CODEX, consumer="office",
                                                  session="office"))["messages"]] == [
                                                      "other session", "to office"]
    assert [m["body"] for m in (await r.room_inbox(CODEX, consumer="orchestra",
                                                  session="orchestra"))["messages"]] == [
                                                      "same session", "to office"]
    assert await inbox_bodies(CLAUDE) == ["same session", "other session"]


async def test_default_consumer_does_not_echo_own_session(sqlite_db):
    await r.room_post("own laptop", CODEX, session="laptop")
    await r.room_post("own cloud", CODEX, session="cloud")
    assert await inbox_bodies(CODEX) == []
    assert await inbox_bodies(CODEX, consumer="laptop") == []
    assert [m["body"] for m in (await r.room_inbox(CODEX, consumer="laptop",
                                                  session="laptop"))["messages"]] == [
                                                      "own cloud"]
    assert [m["body"] for m in (await r.room_inbox(CODEX, consumer="reader",
                                                  session="cloud"))["messages"]] == [
                                                      "own laptop"]


async def test_session_label_used_by_another_agent_is_not_claimed(sqlite_db, caplog):
    await r.room_post("claim", CODEX, session="office")
    await r.room_post("spoof label", CLAUDE, session="office")
    await r.room_post("addressed", CLAUDE, to="office")
    assert "addressed" not in await inbox_bodies(CODEX)
    assert "addressed" not in await inbox_bodies(CLAUDE)
    assert "session labels conflict" in caplog.text


async def test_session_label_matching_another_agent_name_is_not_claimed(sqlite_db,
                                                                         caplog):
    await r.room_post("codex exists", CODEX)
    await r.room_post("spoof", CLAUDE, session="codex")
    await r.room_post("to codex", CODEX, to="codex")
    assert "to codex" not in await inbox_bodies(CLAUDE, session="codex")
    assert "session labels conflict" in caplog.text


async def test_message_filters_history_compact_and_numeric_reply(sqlite_db):
    first = (await r.room_post("x" * 800, CLAUDE, status="request", task_id="T",
                               to="codex"))["message"]["id"]
    await r.room_post("OFFICE_RUNTIME_V1\n{}", CLAUDE, status="info")
    reply = (await r.room_post("reply", CODEX, in_reply_to=first))["message"]
    assert reply["in_reply_to"] == (await r.room_message_get(first))["message"]["message_id"]
    assert await messages.has_reply(room="main", in_reply_to=reply["in_reply_to"],
                                    from_agent="codex", status="info")
    with pytest.raises(ValueError, match="not found"):
        await r.room_post("bad reply", CODEX, in_reply_to=first + 100)
    assert len((await r.room_history())["messages"]) == 3  # legacy full history
    assert (await r.room_history())["messages"][0]["body"] == "x" * 800
    compact = (await r.room_history(compact=True))["messages"]
    assert len(compact) == 2 and compact[0]["truncated"]
    assert len(compact[0]["body"]) == 500
    assert len((await r.room_history(compact=True,
                                     exclude_system=False))["messages"]) == 3
    assert [m["id"] for m in (await r.room_history(after_id=first, kind="info",
                                                    exclude_system=True))["messages"]] == [
                                                        reply["id"]]
    assert [m["id"] for m in (await r.room_history(to="codex", task_id="T",
                                                    kind="request"))["messages"]] == [first]
    assert [m["id"] for m in (await r.room_inbox(CODEX, to="codex", task_id="T",
                                                  kind="request"))["messages"]] == [first]
    with pytest.raises(ValueError, match="not found"):
        await r.room_message_get(first + 100, room="other")


async def test_filtered_inbox_does_not_ack_unselected_messages(sqlite_db):
    info = (await r.room_post("info", CLAUDE, status="info"))["message"]["id"]
    request = (await r.room_post("request", CLAUDE, status="request"))["message"]["id"]
    filtered = await r.room_inbox(CODEX, kind="request")
    assert [m["id"] for m in filtered["messages"]] == [request]
    assert filtered["cursor"] == 0 and filtered["next_cursor"] is None
    assert [m["id"] for m in (await r.room_inbox(CODEX))["messages"]] == [info, request]
    for options in ({"to": "codex"}, {"task_id": "T"}, {"exclude_system": True},
                    {"after_id": 0}, {"since_id": 0}):
        assert (await r.room_inbox(CODEX, **options))["next_cursor"] is None


async def test_system_prefix_filter_escapes_like_wildcards(sqlite_db):
    await r.room_post("OFFICE_RUNTIME_V1\n{}", CLAUDE)
    near_match = (await r.room_post("OFFICEXRUNTIMEXV1\nnot a snapshot", CLAUDE))[
        "message"]["id"]
    assert [m["id"] for m in (await r.room_history(exclude_system=True))[
        "messages"]] == [near_match]


# ─── задачи ──────────────────────────────────────────────────────────────────


async def test_claim_gives_fencing_and_blocks_others(sqlite_db):
    task = (await r.room_task_claim("T1", CLAUDE, title="room", paths=["a.py"]))["task"]
    assert (task["lease_holder"], task["fencing_token"], task["status"]) == (
        "claude", 1, "in_progress")
    with pytest.raises(TaskBusy, match="held by claude"):
        await r.room_task_claim("T1", CODEX)


async def test_reclaim_by_holder_renews_without_new_token(sqlite_db):
    await r.room_task_claim("T1", CLAUDE)
    again = (await r.room_task_claim("T1", CLAUDE, lease_seconds=3600))["task"]
    assert again["fencing_token"] == 1


async def test_expired_lease_is_taken_over_and_old_holder_is_fenced(sqlite_db):
    await r.room_task_claim("T1", CLAUDE)
    await expire_lease("T1")
    taken = (await r.room_task_claim("T1", CODEX))["task"]
    assert (taken["lease_holder"], taken["fencing_token"]) == ("codex", 2)
    with pytest.raises(StaleLease):
        await r.room_task_update("T1", 1, CLAUDE, note="woke up late")
    with pytest.raises(StaleLease):
        await r.room_task_release("T1", 1, CLAUDE)


async def test_expired_lease_rejects_even_the_same_token(sqlite_db):
    await r.room_task_claim("T1", CLAUDE)
    await expire_lease("T1")
    with pytest.raises(StaleLease):
        await r.room_task_update("T1", 1, CLAUDE, note="too late")


async def test_update_and_release_with_current_token(sqlite_db):
    await r.room_task_claim("T1", CLAUDE)
    upd = (await r.room_task_update("T1", 1, CLAUDE, status="blocked", note="waiting",
                                    extend_seconds=600))["task"]
    assert (upd["status"], upd["note"]) == ("blocked", "waiting")
    done = (await r.room_task_release("T1", 1, CLAUDE, note="merged"))["task"]
    assert (done["status"], done["lease_holder"]) == ("done", None)
    with pytest.raises(TaskBusy, match="is done"):
        await r.room_task_claim("T1", CODEX)


async def test_released_to_open_can_be_claimed_by_another(sqlite_db):
    await r.room_task_claim("T1", CLAUDE)
    await r.room_task_release("T1", 1, CLAUDE, status="open")
    assert (await r.room_task_claim("T1", CODEX))["task"]["fencing_token"] == 2


async def test_update_unknown_task(sqlite_db):
    with pytest.raises(TaskNotFound):
        await r.room_task_update("nope", 1, CLAUDE)


async def test_open_task_is_unclaimed_and_listed(sqlite_db):
    opened = await r.room_task_open("T2", CODEX, title="review", paths=["x"])
    assert opened["created"] is True
    assert (await r.room_task_open("T2", CLAUDE))["created"] is False
    listed = (await r.room_tasks(status="open"))["tasks"]
    assert [(t["task_id"], t["created_by"], t["lease_holder"]) for t in listed] == [
        ("T2", "codex", None)]
    await r.room_task_claim("T2", CLAUDE)
    assert (await r.room_tasks(status="open"))["tasks"] == []
