"""Status model fed with rows and events produced by the real vera_shared.room functions."""
from __future__ import annotations

import pytest
from dashboard.office_hourly_service import build_report, status_info_of
from dashboard.office_status import BLOCKER, CONFIRMED, QUEUE, UNCONFIRMED, WORKING
from sqlalchemy import select
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskEventRow, RoomTaskRow
from vera_shared.room import task_wait, tasks
from vera_shared.room.attention import Attention
from vera_shared.timeutil import utc_naive_now

pytestmark = pytest.mark.asyncio

CONTROL_KINDS = ("paused", "review", "resumed", "claimed", "handoff_accept", "unblocked",
                 "released", "blocked", "waiting", "ack_answer")
ATT = Attention("unassigned", "", None, None, None)


async def held(task_id: str, agent: str = "claude") -> int:
    task = await tasks.claim(room="main", task_id=task_id, agent=agent, lease_seconds=600)
    return task["fencing_token"]


async def snapshot(task_id: str):
    async with get_session() as s:
        row = (await s.execute(select(RoomTaskRow).where(RoomTaskRow.task_id == task_id))
               ).scalar_one()
        events = list((await s.execute(select(RoomTaskEventRow).where(
            RoomTaskEventRow.task_id == task_id, RoomTaskEventRow.kind.in_(CONTROL_KINDS)
        ).order_by(RoomTaskEventRow.id.desc()))).scalars())
    return row, (events[0] if events else None)


async def following_event(control: RoomTaskEventRow | None) -> RoomTaskEventRow | None:
    if control is None:
        return None
    async with get_session() as s:
        return (await s.execute(select(RoomTaskEventRow).where(
            RoomTaskEventRow.task_id == control.task_id, RoomTaskEventRow.id > control.id
        ).order_by(RoomTaskEventRow.id).limit(1))).scalar_one_or_none()


async def info_of(task_id: str):
    row, control = await snapshot(task_id)
    return status_info_of(row, ATT, None, utc_naive_now(), control.kind if control else None,
                          None, control, await following_event(control))


async def test_update_blocked_with_note_flip_has_no_text_but_row_note_confirms(sqlite_db):
    token = await held("B1")
    await tasks.update(room="main", task_id="B1", agent="claude", fencing_token=token,
                       status="blocked", note="ждём ключ API")
    _, control = await snapshot("B1")
    assert (control.kind, control.text) == ("blocked", None)
    info = await info_of("B1")
    assert (info.status, info.blocker_confidence) == (BLOCKER, CONFIRMED)
    assert info.wait_reason == "ждём ключ API"


async def test_update_blocked_without_any_reason_is_unconfirmed(sqlite_db):
    token = await held("B2")
    await tasks.update(room="main", task_id="B2", agent="claude", fencing_token=token,
                       status="blocked")
    info = await info_of("B2")
    assert (info.status, info.blocker_confidence) == (BLOCKER, UNCONFIRMED)


async def test_blocked_without_note_ignores_stale_older_note(sqlite_db):
    token = await held("B7")
    await tasks.update(room="main", task_id="B7", agent="claude", fencing_token=token,
                       note="old note")
    await tasks.update(room="main", task_id="B7", agent="claude", fencing_token=token,
                       status="blocked")
    row, _ = await snapshot("B7")
    assert (row.note, row.last_progress_text) == ("old note", "status: blocked")
    info = await info_of("B7")
    assert (info.status, info.blocker_confidence) == (BLOCKER, UNCONFIRMED)


async def test_blocked_with_fresh_note_after_old_note_is_confirmed(sqlite_db):
    token = await held("B8")
    await tasks.update(room="main", task_id="B8", agent="claude", fencing_token=token,
                       note="old note")
    await tasks.update(room="main", task_id="B8", agent="claude", fencing_token=token,
                       status="blocked", note="reason")
    assert (await info_of("B8")).blocker_confidence == CONFIRMED


async def test_unrelated_progress_after_blocked_without_note_does_not_confirm(sqlite_db):
    token = await held("B9")
    await tasks.update(room="main", task_id="B9", agent="claude", fencing_token=token,
                       status="blocked")
    await tasks.update(room="main", task_id="B9", agent="claude", fencing_token=token,
                       note="unrelated progress")
    info = await info_of("B9")
    assert (info.status, info.blocker_confidence) == (BLOCKER, UNCONFIRMED)
    assert info.wait_reason is None


async def test_unrelated_progress_after_blocked_with_note_keeps_bound_reason(sqlite_db):
    token = await held("B10")
    await tasks.update(room="main", task_id="B10", agent="claude", fencing_token=token,
                       status="blocked", note="reason")
    await tasks.update(room="main", task_id="B10", agent="claude", fencing_token=token,
                       note="unrelated progress")
    row, control = await snapshot("B10")
    assert row.last_progress_text == "unrelated progress"
    info = await info_of("B10")
    assert (info.status, info.blocker_confidence) == (BLOCKER, CONFIRMED)
    assert info.wait_reason == "reason"


async def test_release_blocked_with_note_clears_holder_and_confirms(sqlite_db):
    token = await held("B3")
    await tasks.release(room="main", task_id="B3", agent="claude", fencing_token=token,
                        status="blocked", note="нет доступа к серверу")
    row, control = await snapshot("B3")
    assert row.lease_holder is None
    assert (control.kind, control.text, control.data) == (
        "released", "нет доступа к серверу", {"status": "blocked"})
    info = await info_of("B3")
    assert (info.status, info.blocker_confidence) == (BLOCKER, CONFIRMED)


async def test_release_blocked_without_note_is_unconfirmed(sqlite_db):
    token = await held("B4")
    await tasks.release(room="main", task_id="B4", agent="claude", fencing_token=token,
                        status="blocked")
    assert (await info_of("B4")).blocker_confidence == UNCONFIRMED


async def test_release_open_with_note_is_queue_without_confidence(sqlite_db):
    token = await held("B5")
    await tasks.release(room="main", task_id="B5", agent="claude", fencing_token=token,
                        status="open", note="вернул в очередь")
    info = await info_of("B5")
    assert (info.status, info.blocker_confidence) == (QUEUE, None)


async def test_release_by_non_creator_holder_is_unconfirmed(sqlite_db):
    await tasks.open_task(room="main", task_id="B6", agent="vera", title="t", paths=None)
    token = await held("B6", "claude")
    await tasks.release(room="main", task_id="B6", agent="claude", fencing_token=token,
                        status="blocked", note="причина")
    assert (await info_of("B6")).blocker_confidence == UNCONFIRMED


async def test_wait_keeps_working_status_and_fills_independent_fields(sqlite_db):
    token = await held("W1")
    await task_wait.wait(room="main", task_id="W1", agent="claude", fencing_token=token,
                         until_seconds=3600, reason="ждём ответ банка")
    row, control = await snapshot("W1")
    assert (row.status, control.kind, control.text) == ("in_progress", "waiting",
                                                        "ждём ответ банка")
    info = await info_of("W1")
    assert (info.status, info.blocker_confidence) == (WORKING, None)
    assert info.wait_reason == "ждём ответ банка"
    assert info.dependency is not None
    assert info.dependency.startswith("ждём до ")


async def test_card_with_wait_fields_renders_them_while_working(sqlite_db):
    token = await held("W2")
    await task_wait.wait(room="main", task_id="W2", agent="claude", fencing_token=token,
                         until_seconds=3600, reason="ждём ответ банка")
    row, control = await snapshot("W2")
    report = build_report([row], {("main", "W2"): ATT}, [], [], utc_naive_now(), [control])
    card = report.cards[0]
    assert card.status == WORKING
    assert card.info.wait_reason == "ждём ответ банка"
