"""Результат исполнителя срочной задачи → владельцу: один раз, с квитанцией."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from bot_telegram import help_worker, voice_worker
from gateway.voice_command import accept_voice_command
from vera_shared.db.engine import get_session
from vera_shared.db.models_voice import VoiceCommandRow
from vera_shared.room import tasks
from vera_shared.voice_help import delivery_claim
from vera_shared.voice_help import result_state
from vera_shared.voice_help.result_state import PENDING, RESULT_MAX_ERRORS
from voice_help_kit import OWNER, ROOM, SECRET, Recorder, answer, hand_to, help_cmd, queue_row

TASK = "help-vc-help1"


def uses_db(test):
    return pytest.mark.asyncio(pytest.mark.usefixtures("sqlite_db")(test))


@pytest.fixture(autouse=True)
def _test_room(monkeypatch):
    monkeypatch.setenv("VOICE_HELP_ROOM", ROOM)
    monkeypatch.setenv(result_state.SINCE_ENV, "2000-01-01T00:00:00Z")


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


async def _opened(*, brain_delivered: bool = False):
    """Команда открыта. По умолчанию ответ мозга «не доставлен» (заявку снимаем)."""
    await accept_voice_command(help_cmd(), x_internal_secret=SECRET)
    brain = Recorder()
    with patch.object(voice_worker, "ask_brain", AsyncMock(return_value=answer())), \
         patch.object(voice_worker, "save_event", AsyncMock()):
        await voice_worker.process_one(brain, OWNER)
    if not brain_delivered:
        async with get_session() as s:
            row = await s.get(VoiceCommandRow, "vc-help1")
            row.source = {k: v for k, v in (row.source or {}).items() if k != "delivery"}
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
    opened, brain = await _opened(brain_delivered=True)
    assert any("Смотрю логи." in m for m in brain.sent)
    await _finish("ключ sk-ant-api03-" + "A" * 40 + " найден")
    rec = Recorder()
    await help_worker.track_help(rec, opened + timedelta(minutes=1))
    assert not any("Смотрю логи." in m for m in rec.sent)
    assert _results(rec) == []  # ответ мозга считается доставкой
    assert (await queue_row("vc-help1")).help_state == "closed"


@uses_db
async def test_secret_is_redacted_in_result():
    opened, _ = await _opened()
    await _finish("ключ sk-ant-api03-" + "A" * 40 + " найден")
    rec = Recorder()
    await help_worker.track_help(rec, opened + timedelta(minutes=1))
    assert len(_results(rec)) == 1 and "sk-ant-api03" not in _results(rec)[0]


@uses_db
async def test_concurrent_claims_give_exactly_one_winner():
    import asyncio
    await _opened()
    now = datetime(2026, 1, 1)
    out = await asyncio.gather(
        delivery_claim.claim("vc-help1", "brain", now),
        delivery_claim.claim("vc-help1", "executor", now))
    assert sorted(o is None for o in out) == [False, True]


@uses_db
async def test_late_brain_after_executor_sent_does_not_send():
    opened, _ = await _opened()
    await _finish("итог")
    rec = Recorder()
    await help_worker.track_help(rec, opened + timedelta(minutes=1))
    assert len(_results(rec)) == 1
    async with get_session() as s:
        row = await s.get(VoiceCommandRow, "vc-help1")
        row.status, row.answered_at, row.help_state = "pending", None, "closed"
    late = Recorder()
    with patch.object(voice_worker, "ask_brain", AsyncMock(return_value=answer())), \
         patch.object(voice_worker, "save_event", AsyncMock()):
        await voice_worker.process_one(late, OWNER)
    assert late.sent == []


@uses_db
async def test_ambiguous_claim_is_never_resent():
    opened, _ = await _opened()
    await _finish("итог")
    await delivery_claim.claim("vc-help1", "executor", opened)  # упали после отправки
    rec = Recorder()
    await help_worker.track_help(rec, opened + timedelta(minutes=1))
    await help_worker.track_help(rec, opened + timedelta(minutes=2))
    assert rec.sent == []
    assert (await queue_row("vc-help1")).help_state != "closed"


@uses_db
async def test_command_before_cutoff_and_unset_env_send_nothing(monkeypatch):
    opened, _ = await _opened()
    await _finish("итог")
    monkeypatch.setenv(result_state.SINCE_ENV, "2999-01-01T00:00:00Z")
    rec = Recorder()
    await help_worker.track_help(rec, opened + timedelta(minutes=1))
    assert rec.sent == [] and (await queue_row("vc-help1")).help_state == "closed"


@uses_db
async def test_unset_env_disables_feature(monkeypatch):
    opened, _ = await _opened()
    await _finish("итог")
    monkeypatch.delenv(result_state.SINCE_ENV)
    rec = Recorder()
    await help_worker.track_help(rec, opened + timedelta(minutes=1))
    assert rec.sent == [] and (await queue_row("vc-help1")).help_state == "closed"


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
    monkeypatch.setenv(result_state.SINCE_ENV, "2999-01-01T00:00:00Z")
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


@pytest.mark.parametrize("raw,expected", [
    ("", None), ("  ", None), ("мусор", None),
    ("2026-10-27T20:00:00Z", datetime(2026, 10, 27, 20, 0)),
    ("2026-10-27T23:00:00+03:00", datetime(2026, 10, 27, 20, 0)),
    ("2026-10-27T20:00:00", datetime(2026, 10, 27, 20, 0)),
])
def test_parse_since(raw, expected):
    assert result_state.parse_since(raw) == expected


@pytest.mark.parametrize("raw,level,text", [
    (None, logging.INFO, "disabled (VOICE_RESULT_DELIVERY_SINCE not set)"),
    ("bad", logging.WARNING, "disabled"),
    ("2026-10-27T20:00:00Z", logging.INFO, "enabled since 2026-10-27T20:00:00"),
])
def test_startup_log(monkeypatch, caplog, raw, level, text):
    if raw is None:
        monkeypatch.delenv(result_state.SINCE_ENV, raising=False)
    else:
        monkeypatch.setenv(result_state.SINCE_ENV, raw)
    with caplog.at_level(logging.INFO):
        result_state.log_delivery_mode(logging.getLogger("t"))
    rec = caplog.records[-1]
    assert rec.levelno == level and text in rec.getMessage()


async def _brain_failed() -> None:
    """Мозг трижды не ответил: строка в error, владелец ещё не уведомлён."""
    async with get_session() as s:
        row = await s.get(VoiceCommandRow, "vc-help1")
        row.status, row.answered_at, row.notified_at = "error", None, None
        row.source = {k: v for k, v in (row.source or {}).items() if k != "delivery"}


@uses_db
async def test_failed_brain_with_live_holder_sends_status_once():
    await _opened()
    await hand_to(ROOM, TASK, "dot")
    await _brain_failed()
    rec = Recorder()
    assert await voice_worker.notify_failed(rec) == 1
    assert await voice_worker.notify_failed(rec) == 0
    assert rec.sent == [help_worker.assigned_text("dot")]
    row = await queue_row("vc-help1")
    assert row.notified_at is not None and row.source["notice"]["message_id"] == 1


@uses_db
async def test_failed_brain_pending_offer_counts_as_assigned():
    from vera_shared.room import handoff
    await _opened()
    held = await tasks.claim(room=ROOM, task_id=TASK, agent="vera", lease_seconds=900)
    await handoff.offer(room=ROOM, task_id=TASK, agent="vera",
                        fencing_token=held["fencing_token"], to_agent="codex")
    await _brain_failed()
    rec = Recorder()
    await voice_worker.notify_failed(rec)
    assert rec.sent == [help_worker.assigned_text("codex")]


@uses_db
async def test_failed_brain_without_taker_escalates_instead_of_failure():
    from voice_help_kit import messages_in
    await _opened()
    await _brain_failed()
    rec = Recorder()
    await voice_worker.notify_failed(rec)
    await voice_worker.notify_failed(rec)
    assert rec.sent == [help_worker.NO_TAKER_TEXT]
    esc = [m for m in await messages_in(ROOM) if m.to_agent == "dot"]
    assert len(esc) == 1
    assert (await queue_row("vc-help1")).help_state == "escalated"


@uses_db
async def test_failed_brain_blocked_task_keeps_honest_failure():
    await _opened()
    held = await tasks.claim(room=ROOM, task_id=TASK, agent="vera", lease_seconds=900)
    await tasks.release(room=ROOM, task_id=TASK, agent="vera",
                        fencing_token=held["fencing_token"], status="blocked",
                        note="нужен доступ")
    await _brain_failed()
    rec = Recorder()
    await voice_worker.notify_failed(rec)
    assert len(rec.sent) == 1 and rec.sent[0].startswith("Не смогла")


@uses_db
async def test_failed_brain_no_authority_keeps_honest_failure():
    await accept_voice_command(help_cmd(confidence=0.3), x_internal_secret=SECRET)
    with patch.object(voice_worker, "ask_brain", AsyncMock(return_value=answer())), \
         patch.object(voice_worker, "save_event", AsyncMock()):
        await voice_worker.process_one(Recorder(), OWNER)
    await _brain_failed()
    rec = Recorder()
    await voice_worker.notify_failed(rec)
    assert len(rec.sent) == 1 and rec.sent[0].startswith("Не смогла")


@uses_db
async def test_failed_status_send_error_is_retried_once():
    await _opened()
    await hand_to(ROOM, TASK, "dot")
    await _brain_failed()
    rec = Flaky(fail=1)
    await voice_worker.notify_failed(rec)
    assert (await queue_row("vc-help1")).notified_at is None
    await voice_worker.notify_failed(rec)
    await voice_worker.notify_failed(rec)
    assert rec.sent == [help_worker.assigned_text("dot")]
