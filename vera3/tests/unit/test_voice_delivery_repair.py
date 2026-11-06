from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from bot_telegram import help_worker, voice_worker
from gateway.voice_command import accept_voice_command
from vera_shared.db.engine import get_session
from vera_shared.db.models_voice import VoiceCommandRow
from vera_shared.room import tasks
from voice_help_kit import (
    OWNER,
    ROOM,
    SECRET,
    Recorder,
    answer,
    hand_to,
    help_cmd,
    queue_row,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("sqlite_db")]
TASK = "help-vc-help1"


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("VOICE_HELP_ROOM", ROOM)
    monkeypatch.setenv("VOICE_RESULT_DELIVERY_SINCE", "2000-01-01T00:00:00Z")


async def opened():
    await accept_voice_command(help_cmd(), x_internal_secret=SECRET)
    sender = Recorder()
    brain = AsyncMock(return_value=answer())
    with patch.object(voice_worker, "ask_brain", brain), \
            patch.object(voice_worker, "save_event", AsyncMock()):
        await voice_worker.process_one(sender, OWNER)
    return await queue_row("vc-help1"), sender, brain


async def finished(note):
    held = await tasks.claim(room=ROOM, task_id=TASK, agent="codex", lease_seconds=300)
    await tasks.release(room=ROOM, task_id=TASK, agent="codex",
                        fencing_token=held["fencing_token"], status="done", note=note)


async def test_help_has_one_final_owner_and_brain_does_not_run():
    row, sender, brain = await opened()
    brain.assert_not_awaited()
    assert row.answered_at is None and "delivery" not in row.source
    assert row.status == "opened" and len(sender.sent) == 1


async def test_accepted_work_is_not_a_terminal_voice_command():
    row, _, _ = await opened()
    await hand_to(ROOM, TASK, "codex")
    await help_worker.track_help(Recorder(), row.task_opened_at + timedelta(minutes=1))
    current = await queue_row(row.command_id)
    assert current.help_state == "taken" and current.status == "opened"
    assert current.instruction and current.answered_at is None


async def test_room_done_without_final_text_does_not_claim_delivery():
    row, _, _ = await opened()
    await finished(None)
    sender = Recorder()
    await help_worker.track_help(sender, row.task_opened_at + timedelta(minutes=1))
    assert sender.sent == []
    assert (await queue_row(row.command_id)).help_state != "closed"


async def test_legacy_brain_receipt_does_not_silently_discard_executor_result():
    row, _, _ = await opened()
    async with get_session() as s:
        old = await s.get(VoiceCommandRow, row.command_id)
        old.source = {**old.source, "delivery": {
            "owner": "brain", "state": "sent", "message_id": 77}}
    await finished("Improved result, preserve for review; no replay")
    sender = Recorder()
    await help_worker.track_help(sender, row.task_opened_at + timedelta(minutes=1))
    current = await queue_row(row.command_id)
    assert sender.sent == [] and current.help_state == "result_review"
    assert current.source["delivery"]["message_id"] == 77


async def test_missing_cutoff_does_not_forge_delivered_state(monkeypatch):
    row, _, _ = await opened()
    async with get_session() as s:
        current = await s.get(VoiceCommandRow, row.command_id)
        current.source = {k: v for k, v in current.source.items() if k != "delivery"}
    await finished("Result awaiting rollout permission")
    monkeypatch.delenv("VOICE_RESULT_DELIVERY_SINCE")
    sender = Recorder()
    await help_worker.track_help(sender, row.task_opened_at + timedelta(minutes=1))
    assert sender.sent == []
    assert (await queue_row(row.command_id)).help_state != "closed"


async def test_legacy_brain_receipt_suppresses_false_no_taker_escalation():
    row, _, _ = await opened()
    async with get_session() as s:
        current = await s.get(VoiceCommandRow, row.command_id)
        current.source = {**current.source, "delivery": {"owner": "brain", "state": "sent", "message_id": 77}}
    sender = Recorder()
    await help_worker.track_help(sender, row.task_opened_at + timedelta(minutes=5))
    assert sender.sent == [] and (await queue_row(row.command_id)).help_state == "result_review"


async def test_late_hold_does_not_reopen_real_receipt():
    from vera_shared.voice_help.queue_state import hold_command
    row, _, _ = await opened()
    await finished("final")
    await help_worker.track_help(Recorder(), row.task_opened_at + timedelta(minutes=1))
    await hold_command(row.command_id, row.task_opened_at + timedelta(minutes=2))
    current = await queue_row(row.command_id)
    assert current.status == "done" and current.help_state == "closed" and current.answered_at


async def test_status_is_scoped_and_excludes_private_queue_fields(monkeypatch):
    from vera_mcp.room_task_tools import room_task_history
    monkeypatch.setenv("ROOM_NAMES", f"{ROOM},other-room")
    row, _, _ = await opened()
    result = await room_task_history(TASK, ROOM)
    status = result["voice_status"]
    assert status["command_id"] == row.command_id and status["source_verified"]
    assert status["delivery_state"] == "not_started" and status["accepted_executor"] is None
    assert status["provider"] is None and status["model"] is None
    assert not {"instruction", "source", "owner_id", "event_id"} & status.keys()
    assert (await room_task_history(TASK, "other-room"))["voice_status"] is None
    assert (await room_task_history("different-task", ROOM))["voice_status"] is None


async def test_unverified_source_never_delivers_even_if_room_marked_done():
    row, _, _ = await opened()
    async with get_session() as s:
        current = await s.get(VoiceCommandRow, row.command_id)
        current.source = {**current.source, "guard": "other"}
    await finished("untrusted result")
    sender = Recorder()
    await help_worker.track_help(sender, row.task_opened_at + timedelta(minutes=1))
    assert sender.sent == [] and (await queue_row(row.command_id)).help_state == "result_review"


async def test_stale_opened_help_does_not_send_a_competing_stale_answer():
    row, _, brain = await opened()
    async with get_session() as session:
        current = await session.get(VoiceCommandRow, row.command_id)
        current.status, current.acked_at = "pending", None
        current.spoken_at = current.spoken_at - timedelta(minutes=31)
    sender = Recorder()
    with patch.object(voice_worker, "ask_brain", brain):
        await voice_worker.process_one(sender, OWNER)
    current = await queue_row(row.command_id)
    assert sender.sent == [] and current.answered_at is None and current.status == "opened"


async def test_ack_and_offer_do_not_equal_accept_at_five_minutes():
    from vera_shared.room import handoff
    from vera_shared.room.messages import post_message
    row, _, _ = await opened()
    held = await tasks.claim(room=ROOM, task_id=TASK, agent="vera", lease_seconds=900)
    await post_message(room=ROOM, message_id="ack-test", from_agent="dot", status="ack",
                       in_reply_to=f"dispatch-{TASK}", body="Received, not accepted")
    await handoff.offer(room=ROOM, task_id=TASK, agent="vera",
                        fencing_token=held["fencing_token"], to_agent="dot")
    from vera_shared.voice_help.status_read import voice_status_for_task
    status = await voice_status_for_task(ROOM, TASK)
    assert status["dispatch_ack"] and status["accepted_executor"] is None
    sender = Recorder()
    await help_worker.track_help(sender, row.task_opened_at + timedelta(minutes=5))
    assert sender.sent == [help_worker.ESCALATED_TEXT]


@pytest.mark.parametrize("valid", [False, True])
async def test_external_result_requires_fresh_executor_evidence(valid):
    import json

    from vera_shared.voice_help.fact_route import PREFIX
    row, _, _ = await opened()
    async with get_session() as session:
        current = await session.get(VoiceCommandRow, row.command_id)
        current.instruction = "Какой сейчас курс доллара к гривне?"
    now = row.task_opened_at + timedelta(minutes=1)
    note = "An old personal-memory event, not current data"
    if valid:
        note = PREFIX + json.dumps({"available": True, "answer": "Synthetic test observation",
            "checked_at": now.isoformat() + "Z", "as_of": now.isoformat() + "Z",
            "source_url": "https://bank.gov.ua/", "rate_type": "official NBU"})
    await finished(note)
    sender = Recorder()
    await help_worker.track_help(sender, now)
    current = await queue_row(row.command_id)
    assert current.help_state == ("closed" if valid else "result_review")
    assert len(sender.sent) == int(valid)


async def test_late_real_receipt_resolves_unknown_without_second_send():
    from vera_shared.voice_help.delivery_claim import settle
    from vera_shared.voice_help.result_delivery import (
        reserve_result,
        store_result_receipt,
    )
    row, _, _ = await opened()
    now = row.task_opened_at
    assert await reserve_result(row.command_id, now) is None
    sender = Recorder()
    await help_worker.track_help(sender, now + timedelta(minutes=6))
    assert (await queue_row(row.command_id)).help_state == "result_unknown"
    assert await store_result_receipt(row.command_id, 801, now + timedelta(minutes=7))
    await settle(row.command_id, "executor", "error")
    await help_worker.track_help(sender, now + timedelta(minutes=8))
    current = await queue_row(row.command_id)
    assert sender.sent == [] and current.source["delivery"]["state"] == "sent"
    assert current.source["delivery"]["message_id"] == 801 and current.status == "done"


async def test_restart_after_opening_message_commit_uses_identical_body():
    from voice_help_kit import messages_in
    await accept_voice_command(help_cmd(instruction="курс USD UAH"), x_internal_secret=SECRET)
    with patch.object(voice_worker, "mark_opened", AsyncMock(side_effect=RuntimeError("interrupted"))):
        await voice_worker.process_one(Recorder(), OWNER)
    [first] = await messages_in(ROOM)
    async with get_session() as session:
        row = await session.get(VoiceCommandRow, "vc-help1")
        row.next_attempt_at = None
        later = row.created_at + timedelta(minutes=1)
    with patch.object(voice_worker, "utc_naive_now", return_value=later):
        await voice_worker.process_one(Recorder(), OWNER)
    [again] = await messages_in(ROOM)
    assert again.body == first.body
    assert (await queue_row("vc-help1")).help_state == "opened"


@pytest.mark.parametrize("uncertain", [False, True])
async def test_long_request_keeps_last_constraints_in_room_body(uncertain):
    from voice_help_kit import messages_in, tasks_in
    instruction = "курс USD UAH " + "x" * 1700 + " NEVER PUBLISH OR SPEND."
    await accept_voice_command(help_cmd(instruction=instruction, confidence=0.3 if uncertain else 0.95),
                               x_internal_secret=SECRET)
    await voice_worker.process_one(Recorder(), OWNER)
    [task] = await tasks_in(ROOM)
    post = next(m for m in await messages_in(ROOM) if m.message_id == TASK)
    assert instruction in post.body and "FRESH_FACTS_V1" in post.body
    assert len(task.next_action) <= 2000
    assert "Read the complete original request" in task.next_action
