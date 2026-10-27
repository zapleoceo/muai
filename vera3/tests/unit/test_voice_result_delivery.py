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
from vera_shared.voice_help import delivery_claim, result_state
from vera_shared.voice_help.result_state import PENDING, RESULT_MAX_ERRORS
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

TASK = "help-vc-help1"


def uses_db(test):
    return pytest.mark.asyncio(pytest.mark.usefixtures("sqlite_db")(test))


@pytest.fixture(autouse=True)
def _test_room(monkeypatch):
    monkeypatch.setenv("VOICE_HELP_ROOM", ROOM)
    monkeypatch.setenv(result_state.SINCE_ENV, "2000-01-01T00:00:00Z")


class TelegramBadRequest(Exception):  # имя класса = признак точного отказа Telegram
    pass


class Flaky(Recorder):
    def __init__(self, fail: int = 0, ret: int | None = None,
                 exc: type[Exception] = TelegramBadRequest) -> None:
        super().__init__()
        self.fail, self.ret, self.calls, self.exc = fail, ret, 0, exc

    async def __call__(self, html: str, plain: str) -> int:
        self.calls += 1
        if self.calls <= self.fail:
            raise self.exc("telegram down")
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
    for i in range(1, 4):
        await help_worker.track_help(rec, opened + timedelta(minutes=i))
    row = await queue_row("vc-help1")
    # пустой message_id — не доказательство недоставки: unknown, без автоповтора
    assert row.help_state == "result_unknown" and rec.calls == 1
    assert "result_message_id" not in (row.source or {})


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
    assert not any("Смотрю логи." in m for m in late.sent)  # финала нет; статус допустим


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
async def test_failed_brain_pending_offer_is_offered_not_working():
    from vera_shared.room import handoff
    await _opened()
    held = await tasks.claim(room=ROOM, task_id=TASK, agent="vera", lease_seconds=900)
    await handoff.offer(room=ROOM, task_id=TASK, agent="vera",
                        fencing_token=held["fencing_token"], to_agent="codex")
    await _brain_failed()
    rec = Recorder()
    await voice_worker.notify_failed(rec)
    assert rec.sent == [help_worker.offered_text("codex")]
    assert "жду принятия" in rec.sent[0] and "в работе" not in rec.sent[0]


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


class ReadTimeout(Exception):
    pass


@uses_db
async def test_ambiguous_timeout_on_result_is_unknown_not_retried():
    opened, _ = await _opened()
    await _finish("итог")
    rec = Flaky(fail=99, exc=ReadTimeout)
    for i in range(1, 4):
        await help_worker.track_help(rec, opened + timedelta(minutes=i))
    row = await queue_row("vc-help1")
    assert rec.calls == 1 and row.help_state == "result_unknown"
    assert row.source["delivery"]["state"] == "unknown"


@uses_db
async def test_classification():
    from vera_shared.voice_help.delivery_claim import classify_send_error as c
    assert c(TelegramBadRequest()) == "error" and c(ConnectionRefusedError()) == "error"
    assert c(RuntimeError("OWNER_TELEGRAM_ID not set — refusing to send")) == "error"
    assert c(ReadTimeout()) == "unknown" and c(TimeoutError()) == "unknown"
    assert c(RuntimeError("boom")) == "unknown"


async def _open_with(side_effect=None):
    await accept_voice_command(help_cmd(), x_internal_secret=SECRET)
    rec = Recorder()
    brain = AsyncMock(return_value=answer(), side_effect=side_effect)
    with patch.object(voice_worker, "ask_brain", brain), \
         patch.object(voice_worker, "save_event", AsyncMock()):
        await voice_worker.process_one(rec, OWNER)
    return rec


@uses_db
async def test_before_accept_brain_final_then_executor_sends_nothing():
    rec = await _open_with()
    assert any("Смотрю логи." in m for m in rec.sent)
    row = await queue_row("vc-help1")
    assert row.answered_at is not None and row.source["delivery"]["state"] == "sent"
    await _finish("итог")
    out = Recorder()
    opened = row.task_opened_at
    await help_worker.track_help(out, opened + timedelta(minutes=1))
    assert out.sent == []


@uses_db
async def test_after_accept_brain_is_status_only_and_executor_owns_final():
    async def accept_during_brain(*_a, **_k):
        await hand_to(ROOM, TASK, "codex")
        return answer()

    rec = await _open_with(side_effect=accept_during_brain)
    assert not any("Смотрю логи." in m for m in rec.sent)
    assert rec.sent[-1] == "Мозг ответил, задачу ведёт codex."
    row = await queue_row("vc-help1")
    assert row.answered_at is None
    assert "delivery" not in row.source and row.source["progress"]["state"] == "sent"
    await _finish("итог")
    out = Recorder()
    await help_worker.track_help(out, row.task_opened_at + timedelta(minutes=1))
    await help_worker.track_help(out, row.task_opened_at + timedelta(minutes=2))
    assert len(_results(out)) == 1


@uses_db
async def test_executor_reserved_final_brain_does_not_send_or_mark_answered():
    async def executor_reserves(*_a, **_k):
        await delivery_claim.claim("vc-help1", "executor", datetime(2026, 1, 1))
        return answer()

    rec = await _open_with(side_effect=executor_reserves)
    assert not any("Смотрю логи." in m for m in rec.sent)
    row = await queue_row("vc-help1")
    assert row.answered_at is None and row.source["delivery"]["owner"] == "executor"


@uses_db
async def test_brain_ambiguous_send_is_unknown_without_answered_or_retry():
    await accept_voice_command(help_cmd(), x_internal_secret=SECRET)

    async def timeout_send(_h, _p):
        if "Открыл задачу" in _p or "Услышала" in _p:
            return 1
        raise ReadTimeout("after request")

    sends = []

    async def spy(h, p):
        sends.append(p)
        return await timeout_send(h, p)

    with patch.object(voice_worker, "ask_brain", AsyncMock(return_value=answer())), \
         patch.object(voice_worker, "save_event", AsyncMock()):
        await voice_worker.process_one(spy, OWNER)
        await voice_worker.process_one(spy, OWNER)
    row = await queue_row("vc-help1")
    assert row.answered_at is None and row.source["delivery"]["state"] == "unknown"
    assert row.status != "pending"
    assert len([p for p in sends if "Смотрю логи" in p]) == 1


@uses_db
async def test_brain_definite_failure_is_error_and_retryable():
    await accept_voice_command(help_cmd(), x_internal_secret=SECRET)

    async def refuse(_h, p):
        if "Смотрю логи" in p:
            raise TelegramBadRequest("bad")
        return 1

    with patch.object(voice_worker, "ask_brain", AsyncMock(return_value=answer())), \
         patch.object(voice_worker, "save_event", AsyncMock()):
        await voice_worker.process_one(refuse, OWNER)
    row = await queue_row("vc-help1")
    assert row.source["delivery"]["state"] == "error" and row.status == "pending"


def test_compose_has_result_since_in_bot_telegram_only():
    from pathlib import Path

    import yaml
    compose = yaml.safe_load(
        (Path(__file__).parents[2] / "infra" / "docker-compose.yml").read_text(encoding="utf-8"))
    holders = [n for n, svc in compose["services"].items()
               if "VOICE_RESULT_DELIVERY_SINCE" in (svc.get("environment") or {})]
    assert holders == ["bot-telegram"]


async def _set_delivery(state_row_help: str | None = None, **source) -> None:
    async with get_session() as s:
        row = await s.get(VoiceCommandRow, "vc-help1")
        row.source = {**(row.source or {}), **source}
        if state_row_help:
            row.help_state = state_row_help


@uses_db
@pytest.mark.parametrize("help_state,source", [
    ("closed", {"delivery": {"owner": "executor", "state": "sent", "message_id": 5}}),
    ("result_pending", {"delivery": {"owner": "executor", "state": "claimed"}}),
    ("result_unknown", {"delivery": {"owner": "executor", "state": "unknown"}}),
    ("result_error", {"delivery": {"owner": "executor", "state": "error"}}),
])
async def test_done_task_with_executor_final_gets_no_failure(help_state, source):
    await _opened()
    await _finish("итог")
    await _brain_failed()
    await _set_delivery(help_state, **source)
    rec = Recorder()
    await voice_worker.notify_failed(rec)
    assert rec.sent == []
    assert (await queue_row("vc-help1")).notified_at is not None


@uses_db
async def test_done_task_without_row_final_yet_gets_no_failure():
    await _opened()
    await _finish("итог")
    await _brain_failed()
    rec = Recorder()
    await voice_worker.notify_failed(rec)
    assert rec.sent == []


@uses_db
async def test_cancelled_task_without_final_keeps_failure():
    await _opened()
    held = await tasks.claim(room=ROOM, task_id=TASK, agent="vera", lease_seconds=900)
    from vera_shared.room import task_cancel
    await task_cancel.cancel(room=ROOM, task_id=TASK, agent="vera",
                             fencing_token=held["fencing_token"], reason="не нужно")
    await _brain_failed()
    rec = Recorder()
    await voice_worker.notify_failed(rec)
    assert len(rec.sent) == 1 and rec.sent[0].startswith("Не смогла")


@uses_db
async def test_accept_before_brain_claim_gives_executor_verdict():
    from vera_shared.room import handoff
    await _opened()
    held = await tasks.claim(room=ROOM, task_id=TASK, agent="vera", lease_seconds=900)
    await handoff.offer(room=ROOM, task_id=TASK, agent="vera",
                        fencing_token=held["fencing_token"], to_agent="codex")
    await handoff.accept(room=ROOM, task_id=TASK, agent="codex")
    verdict = await delivery_claim.claim_unless_accepted(
        "vc-help1", ROOM, TASK, "brain", datetime(2026, 1, 1))
    assert verdict == ("executor", "codex")
    assert "delivery" not in ((await queue_row("vc-help1")).source or {})


@uses_db
async def test_brain_claim_before_accept_wins_and_executor_then_sends_nothing():
    from vera_shared.room import handoff
    opened, _ = await _opened()
    verdict = await delivery_claim.claim_unless_accepted(
        "vc-help1", ROOM, TASK, "brain", datetime(2026, 1, 1))
    assert verdict == ("won", None)
    held = await tasks.claim(room=ROOM, task_id=TASK, agent="vera", lease_seconds=900)
    await handoff.offer(room=ROOM, task_id=TASK, agent="vera",
                        fencing_token=held["fencing_token"], to_agent="codex")
    await handoff.accept(room=ROOM, task_id=TASK, agent="codex")
    await delivery_claim.settle("vc-help1", "brain", "sent", 9)
    await _finish("итог")
    out = Recorder()
    await help_worker.track_help(out, opened + timedelta(minutes=1))
    assert _results(out) == []


async def _offer_accept_done(note: str = "итог исполнителя") -> None:
    from vera_shared.room import handoff
    held = await tasks.claim(room=ROOM, task_id=TASK, agent="vera", lease_seconds=900)
    await handoff.offer(room=ROOM, task_id=TASK, agent="vera",
                        fencing_token=held["fencing_token"], to_agent="codex")
    await handoff.accept(room=ROOM, task_id=TASK, agent="codex")
    await _finish(note, agent="codex")


@uses_db
async def test_after_done_lease_released_brain_still_loses_to_executor():
    await _opened()
    await _offer_accept_done()
    verdict = await delivery_claim.claim_unless_accepted(
        "vc-help1", ROOM, TASK, "brain", datetime(2026, 1, 1))
    assert verdict == ("executor", "codex")
    assert "delivery" not in ((await queue_row("vc-help1")).source or {})


@uses_db
async def test_executor_result_sent_once_after_done_and_late_brain():
    opened, _ = await _opened()
    await _offer_accept_done("текст исполнителя")
    late = Recorder()
    with patch.object(voice_worker, "ask_brain", AsyncMock(return_value=answer())), \
         patch.object(voice_worker, "save_event", AsyncMock()):
        async with get_session() as s:
            row = await s.get(VoiceCommandRow, "vc-help1")
            row.status, row.answered_at = "pending", None
            row.source = {k: v for k, v in (row.source or {}).items() if k != "delivery"}
        await voice_worker.process_one(late, OWNER)
    assert not any("Смотрю логи." in m for m in late.sent)
    out = Recorder()
    await help_worker.track_help(out, opened + timedelta(minutes=1))
    await help_worker.track_help(out, opened + timedelta(minutes=2))
    assert _results(out) == [f"Результат по задаче {TASK} (codex): текст исполнителя"]


@uses_db
async def test_lost_claim_does_not_move_row_to_pending():
    opened, _ = await _opened()
    await _finish("итог")
    await delivery_claim.claim("vc-help1", "brain", opened)  # claimed, исход неизвестен
    out = Recorder()
    await help_worker.track_help(out, opened + timedelta(minutes=1))
    row = await queue_row("vc-help1")
    assert out.sent == [] and row.help_state == "opened"


@uses_db
async def test_executor_takes_over_definite_brain_error():
    opened, _ = await _opened()
    await _finish("итог")
    await delivery_claim.claim("vc-help1", "brain", opened)
    await delivery_claim.settle("vc-help1", "brain", "error")
    out = Recorder()
    await help_worker.track_help(out, opened + timedelta(minutes=1))
    assert len(_results(out)) == 1
