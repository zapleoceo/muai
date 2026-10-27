"""Результат исполнителя срочной задачи → владельцу: один раз, с квитанцией."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from bot_telegram import help_worker, voice_worker
from gateway.voice_command import accept_voice_command
from vera_shared.room import tasks
from vera_shared.voice_help import result_state
from vera_shared.voice_help.result_state import PENDING, RESULT_MAX_ERRORS
from voice_help_kit import OWNER, ROOM, SECRET, Recorder, answer, hand_to, help_cmd, queue_row

TASK = "help-vc-help1"


def uses_db(test):
    return pytest.mark.asyncio(pytest.mark.usefixtures("sqlite_db")(test))


@pytest.fixture(autouse=True)
def _test_room(monkeypatch):
    monkeypatch.setenv("VOICE_HELP_ROOM", ROOM)


class Flaky(Recorder):
    def __init__(self, fail: int = 0, ret: int | None = None) -> None:
        super().__init__()
        self.fail, self.ret, self.calls = fail, ret, 0

    async def __call__(self, html: str, plain: str) -> int:
        self.calls += 1
        if self.calls <= self.fail:
            raise RuntimeError("telegram down")
        if self.ret is not None:
            return self.ret
        return await super().__call__(html, plain)


async def _opened():
    await accept_voice_command(help_cmd(), x_internal_secret=SECRET)
    brain = Recorder()
    with patch.object(voice_worker, "ask_brain", AsyncMock(return_value=answer())), \
         patch.object(voice_worker, "save_event", AsyncMock()):
        await voice_worker.process_one(brain, OWNER)
    return (await queue_row("vc-help1")).task_opened_at, brain


async def _finish(note: str | None, agent: str = "codex") -> None:
    held = await tasks.claim(room=ROOM, task_id=TASK, agent=agent, lease_seconds=900)
    await tasks.release(room=ROOM, task_id=TASK, agent=agent,
                        fencing_token=held["fencing_token"], status="done", note=note)


def _results(rec: Recorder) -> list[str]:
    return [m for m in rec.sent if m.startswith("Результат по задаче")]


@uses_db
async def test_result_sent_once_across_ticks_and_restart():
    opened, _ = await _opened()
    await _finish("готово: логи чистые")
    rec = Recorder()
    await help_worker.track_help(rec, opened + timedelta(minutes=1))
    await help_worker.track_help(rec, opened + timedelta(minutes=2))
    assert _results(rec) == [f"Результат по задаче {TASK} (codex): готово: логи чистые"]
    row = await queue_row("vc-help1")
    assert row.source["result_message_id"] == 1 and row.help_state == "closed"
    assert row.status == "done"


@uses_db
async def test_taken_then_done_delivers_result():
    opened, _ = await _opened()
    await hand_to(ROOM, TASK, "codex")
    rec = Recorder()
    await help_worker.track_help(rec, opened + timedelta(minutes=1))
    await help_worker.track_help(rec, opened + timedelta(minutes=2))
    assert rec.sent == [help_worker.taken_text("codex")]
    await _finish("сделано")
    await help_worker.track_help(rec, opened + timedelta(minutes=3))
    await help_worker.track_help(rec, opened + timedelta(minutes=4))
    assert len(_results(rec)) == 1
    assert (await queue_row("vc-help1")).help_state == "closed"


@uses_db
async def test_failed_send_keeps_row_then_one_receipt():
    opened, _ = await _opened()
    await _finish("итог")
    rec = Flaky(fail=1)
    await help_worker.track_help(rec, opened + timedelta(minutes=1))
    row = await queue_row("vc-help1")
    assert row.help_state == "result_error" and row.source["result_errors"] == 1
    await help_worker.track_help(rec, opened + timedelta(minutes=2))
    await help_worker.track_help(rec, opened + timedelta(minutes=3))
    assert len(_results(rec)) == 1 and rec.calls == 2
    assert (await queue_row("vc-help1")).help_state == "closed"


@uses_db
async def test_no_receipt_means_not_closed():
    opened, _ = await _opened()
    await _finish("итог")
    rec = Flaky(ret=0)
    await help_worker.track_help(rec, opened + timedelta(minutes=1))
    row = await queue_row("vc-help1")
    assert row.help_state in ("result_error", "result_pending") and not (row.source or {}).get("result_message_id")


@uses_db
async def test_attempts_are_bounded_and_never_closed_without_receipt():
    opened, _ = await _opened()
    await _finish("итог")
    rec = Flaky(fail=999)
    for i in range(RESULT_MAX_ERRORS + 3):
        await help_worker.track_help(rec, opened + timedelta(minutes=1 + i))
    assert rec.calls == RESULT_MAX_ERRORS
    row = await queue_row("vc-help1")
    assert row.help_state in ("result_error", "result_pending") and not (row.source or {}).get("result_message_id")


@uses_db
async def test_owner_unset_send_raises_fail_closed():
    opened, _ = await _opened()
    await _finish("итог")

    async def refuse(_h: str, _p: str) -> int:
        raise RuntimeError("OWNER_TELEGRAM_ID not set — refusing to send")

    await help_worker.track_help(refuse, opened + timedelta(minutes=1))
    row = await queue_row("vc-help1")
    assert row.help_state in ("result_error", "result_pending") and not (row.source or {}).get("result_message_id")


@uses_db
@pytest.mark.parametrize("note", [None, "   "])
async def test_empty_result_not_sent_and_closed(note):
    opened, _ = await _opened()
    await _finish(note)
    rec = Recorder()
    await help_worker.track_help(rec, opened + timedelta(minutes=1))
    assert _results(rec) == []
    row = await queue_row("vc-help1")
    assert row.help_state == "closed" and "result_message_id" not in (row.source or {})


@uses_db
async def test_brain_answer_is_not_resent_and_secret_is_redacted():
    opened, brain = await _opened()
    assert any("Смотрю логи." in m for m in brain.sent)
    await _finish("ключ sk-ant-api03-" + "A" * 40 + " найден")
    rec = Recorder()
    await help_worker.track_help(rec, opened + timedelta(minutes=1))
    assert not any("Смотрю логи." in m for m in rec.sent)
    assert len(_results(rec)) == 1 and "sk-ant-api03" not in _results(rec)[0]


@uses_db
async def test_crash_after_send_is_unknown_and_never_resent():
    opened, _ = await _opened()
    await _finish("итог")
    await result_state.move("vc-help1", ("opened",), PENDING, opened)  # упали после намерения
    rec = Recorder()
    await help_worker.track_help(rec, opened + timedelta(minutes=1))
    await help_worker.track_help(rec, opened + timedelta(minutes=2))
    assert rec.sent == []
    assert (await queue_row("vc-help1")).help_state == "result_unknown"


@uses_db
async def test_intent_is_committed_before_send():
    opened, _ = await _opened()
    await _finish("итог")
    seen: list[str] = []

    async def spy(_h: str, _p: str) -> int:
        seen.append((await queue_row("vc-help1")).help_state)
        return 7

    await help_worker.track_help(spy, opened + timedelta(minutes=1))
    assert seen == ["result_pending"]
    assert (await queue_row("vc-help1")).help_state == "closed"


@uses_db
async def test_results_before_rollout_are_not_resent(monkeypatch):
    opened, _ = await _opened()
    await _finish("старый итог")
    monkeypatch.setattr(result_state, "RESULT_DELIVERY_SINCE", opened + timedelta(days=1))
    rec = Recorder()
    await help_worker.track_help(rec, opened + timedelta(minutes=1))
    assert rec.sent == []
    assert (await queue_row("vc-help1")).help_state == "closed"


@uses_db
async def test_already_closed_row_is_untouched():
    opened, _ = await _opened()
    await _finish("итог")
    rec = Recorder()
    await help_worker.track_help(rec, opened + timedelta(minutes=1))
    rec.sent.clear()
    await help_worker.track_help(rec, opened + timedelta(minutes=2))
    assert rec.sent == []


def test_result_text_is_bounded():
    from bot_telegram.formatting import TELEGRAM_MAX_CHARS
    assert len(help_worker.result_text(TASK, "codex", "я" * 9000)) <= TELEGRAM_MAX_CHARS
