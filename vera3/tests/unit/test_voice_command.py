"""Голосовое поручение: приём в шлюзе → очередь → ответ владельцу в Telegram.

Закрепляем: без секрета шлюз не пишет ничего; ретрай слушателя не задваивает
поручение; бот отвечает двумя сообщениями (подтверждение и результат) тем же
путём, что на текст; адресат — только владелец, а без него воркер молчит.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from gateway.voice_command import VoiceCommand, accept_voice_command
from pydantic import ValidationError
from sqlalchemy import select, update
from vera_shared import voice_commands
from vera_shared.db.models import EventRow
from vera_shared.db.models_voice import VoiceCommandRow

from bot_telegram import voice_worker
from bot_telegram.brain import BrainAnswer

SECRET = "test-internal-secret"
OWNER = 169510539
TEST_INSTRUCTION = "срочно напиши мне что-то в телеграм"


def _cmd(command_id: str = "vc-1", instruction: str = TEST_INSTRUCTION,
         spoken_at: datetime | None = None) -> VoiceCommand:
    # Время — «сейчас», а не дата-константа: у бота есть срок годности
    # поручения (MAX_AGE), и тест с фиксированной датой начал бы падать сам
    # собой через полчаса после неё.
    return VoiceCommand(command_id=command_id, instruction=instruction,
                        spoken_at=spoken_at or datetime.now(timezone.utc),
                        app="zoom.exe", window_title="Созвон")


async def _rows(get_session, model):
    async with get_session() as s:
        return list((await s.execute(select(model))).scalars())


class TestGateway:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("secret", [None, "", "wrong"])
    async def test_without_secret_nothing_is_written(self, sqlite_db, secret):
        with pytest.raises(HTTPException) as exc:
            await accept_voice_command(_cmd(), x_internal_secret=secret)
        assert exc.value.status_code == 401
        assert await _rows(sqlite_db, VoiceCommandRow) == []
        assert await _rows(sqlite_db, EventRow) == []

    @pytest.mark.asyncio
    async def test_command_becomes_event_and_queue_row(self, sqlite_db):
        result = await accept_voice_command(_cmd(), x_internal_secret=SECRET)
        assert result.ok and not result.deduped
        [event] = await _rows(sqlite_db, EventRow)
        assert event.id == result.event_id
        assert event.source == "voice_command"
        assert TEST_INSTRUCTION in event.content_text
        assert event.triage_status == "pending"          # попадёт в поиск
        [row] = await _rows(sqlite_db, VoiceCommandRow)
        assert (row.status, row.event_id) == ("pending", event.id)

    @pytest.mark.asyncio
    async def test_retry_of_same_command_is_deduped(self, sqlite_db):
        first = await accept_voice_command(_cmd(), x_internal_secret=SECRET)
        again = await accept_voice_command(_cmd(), x_internal_secret=SECRET)
        assert again.deduped and again.event_id == first.event_id
        assert len(await _rows(sqlite_db, EventRow)) == 1
        assert len(await _rows(sqlite_db, VoiceCommandRow)) == 1

    def test_empty_instruction_is_rejected(self):
        with pytest.raises(ValidationError):
            _cmd(instruction="")


async def _set_row(get_session, **values) -> None:
    async with get_session() as s:
        await s.execute(update(VoiceCommandRow).values(**values))


async def _make_due(get_session) -> None:
    """Пауза ретрая истекла — не ждать её в тесте по-настоящему."""
    await _set_row(get_session, next_attempt_at=None)


def _answer() -> BrainAnswer:
    return BrainAnswer(raw="Пишу: всё в порядке.", provider="p", cost_usd=0.0,
                       n_results=0, n_history=0)


class _Send:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, html: str, plain: str) -> int:
        self.sent.append(plain)
        return len(self.sent)


class TestWorker:
    @pytest.mark.asyncio
    async def test_owner_gets_ack_then_result(self, sqlite_db):
        await accept_voice_command(_cmd(), x_internal_secret=SECRET)
        send = _Send()
        ask = AsyncMock(return_value=_answer())
        with patch.object(voice_worker, "ask_brain", ask), \
             patch.object(voice_worker, "save_event", AsyncMock()) as saved:
            assert await voice_worker.process_one(send, OWNER) is True
            assert await voice_worker.process_one(send, OWNER) is False
        assert send.sent[0] == f"Услышала: „{TEST_INSTRUCTION}“. Делаю."
        assert send.sent[1].startswith("Пишу: всё в порядке.")
        assert len(send.sent) == 2
        # Тот же путь, что у текстового сообщения: вопрос мозгу — текстом
        # поручения, в чате владельца; ответ записан событием.
        ask.assert_awaited_once_with(TEST_INSTRUCTION, OWNER, OWNER)
        saved.assert_awaited_once()
        [row] = await _rows(sqlite_db, VoiceCommandRow)
        assert row.status == "done" and row.instruction == ""

    @pytest.mark.asyncio
    async def test_late_command_is_reported_not_executed(self, sqlite_db):
        spoken = datetime.now(timezone.utc) - timedelta(hours=2)
        await accept_voice_command(_cmd(spoken_at=spoken), x_internal_secret=SECRET)
        send = _Send()
        ask = AsyncMock(return_value=_answer())
        with patch.object(voice_worker, "ask_brain", ask),              patch.object(voice_worker, "save_event", AsyncMock()):
            assert await voice_worker.process_one(send, OWNER) is True
        ask.assert_not_awaited()
        assert len(send.sent) == 1
        assert send.sent[0].startswith("Поручение дошло с опозданием (120 мин)")
        assert TEST_INSTRUCTION in send.sent[0]
        [row] = await _rows(sqlite_db, VoiceCommandRow)
        assert row.status == "done"

    @pytest.mark.asyncio
    async def test_fresh_command_within_limit_is_executed(self, sqlite_db):
        spoken = datetime.now(timezone.utc) - timedelta(minutes=10)
        await accept_voice_command(_cmd(spoken_at=spoken), x_internal_secret=SECRET)
        send = _Send()
        ask = AsyncMock(return_value=_answer())
        with patch.object(voice_worker, "ask_brain", ask),              patch.object(voice_worker, "save_event", AsyncMock()):
            await voice_worker.process_one(send, OWNER)
        ask.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failed_answer_is_retried_without_second_ack(self, sqlite_db):
        await accept_voice_command(_cmd(), x_internal_secret=SECRET)
        send = _Send()
        ask = AsyncMock(side_effect=[RuntimeError("search down"), _answer()])
        with patch.object(voice_worker, "ask_brain", ask), \
             patch.object(voice_worker, "save_event", AsyncMock()):
            await voice_worker.process_one(send, OWNER)
            [row] = await _rows(sqlite_db, VoiceCommandRow)
            assert row.status == "pending"
            await _make_due(sqlite_db)
            await voice_worker.process_one(send, OWNER)
        assert sum(m.startswith("Услышала") for m in send.sent) == 1
        [row] = await _rows(sqlite_db, VoiceCommandRow)
        assert row.status == "done"

    @pytest.mark.asyncio
    async def test_retry_waits_instead_of_running_back_to_back(self, sqlite_db):
        await accept_voice_command(_cmd(), x_internal_secret=SECRET)
        ask = AsyncMock(side_effect=RuntimeError("search down"))
        with patch.object(voice_worker, "ask_brain", ask), \
             patch.object(voice_worker, "save_event", AsyncMock()):
            assert await voice_worker.process_one(_Send(), OWNER) is True
            assert await voice_worker.process_one(_Send(), OWNER) is False
        assert ask.await_count == 1

    @pytest.mark.asyncio
    async def test_last_failure_tells_owner_and_clears_text(self, sqlite_db):
        await accept_voice_command(_cmd(), x_internal_secret=SECRET)
        send = _Send()
        ask = AsyncMock(side_effect=RuntimeError(f"search down: {TEST_INSTRUCTION}"))
        with patch.object(voice_worker, "ask_brain", ask), \
             patch.object(voice_worker, "save_event", AsyncMock()):
            for _ in range(3):
                await _make_due(sqlite_db)
                await voice_worker.process_one(send, OWNER)
        await voice_worker.notify_failed(send)
        [row] = await _rows(sqlite_db, VoiceCommandRow)
        assert (row.status, row.instruction) == ("error", "")
        assert row.notified_at is not None
        assert send.sent[-1] == f"Не смогла выполнить поручение: „{TEST_INSTRUCTION}“."
        # В колонку error — только тип, не текст исключения с поручением.
        assert row.error == "RuntimeError"

    @pytest.mark.asyncio
    async def test_failed_notification_is_retried_next_pass(self, sqlite_db):
        await accept_voice_command(_cmd(), x_internal_secret=SECRET)
        await _set_row(sqlite_db, status="error", attempts=3)
        flaky = AsyncMock(side_effect=[RuntimeError("telegram down"), 1])
        assert await voice_worker.notify_failed(flaky) == 0
        [row] = await _rows(sqlite_db, VoiceCommandRow)
        assert row.notified_at is None and row.instruction == TEST_INSTRUCTION
        assert await voice_worker.notify_failed(flaky) == 1
        assert await voice_worker.notify_failed(flaky) == 0
        assert flaky.await_count == 2

    @pytest.mark.asyncio
    async def test_one_failed_notification_does_not_stop_the_others(self, sqlite_db):
        await accept_voice_command(_cmd("vc-1"), x_internal_secret=SECRET)
        await accept_voice_command(_cmd("vc-2"), x_internal_secret=SECRET)
        await _set_row(sqlite_db, status="error", attempts=3)
        flaky = AsyncMock(side_effect=[RuntimeError("telegram down"), 1])
        assert await voice_worker.notify_failed(flaky) == 1

    @pytest.mark.asyncio
    async def test_no_failure_message_over_a_delivered_answer(self, sqlite_db):
        """Ответ ушёл, а упала запись события на последней попытке."""
        await accept_voice_command(_cmd(), x_internal_secret=SECRET)
        await _set_row(sqlite_db, attempts=2)
        send = _Send()
        with patch.object(voice_worker, "ask_brain", AsyncMock(return_value=_answer())), \
             patch.object(voice_worker, "save_event",
                          AsyncMock(side_effect=RuntimeError("gateway down"))):
            await voice_worker.process_one(send, OWNER)
        await voice_worker.notify_failed(send)
        assert not any(m.startswith("Не смогла") for m in send.sent)
        [row] = await _rows(sqlite_db, VoiceCommandRow)
        assert row.status == "error" and row.notified_at is not None

    @pytest.mark.asyncio
    async def test_command_that_crashes_every_time_ends_in_error(self, sqlite_db):
        """Процесс падает посреди поручения — строка висит в processing."""
        await accept_voice_command(_cmd(), x_internal_secret=SECRET)
        await _set_row(sqlite_db, status="processing", attempts=3,
                       updated_at=datetime(2026, 1, 1))
        send = _Send()
        assert await voice_commands.revive_stale() == 1
        await voice_worker.notify_failed(send)
        [row] = await _rows(sqlite_db, VoiceCommandRow)
        assert (row.status, row.instruction) == ("error", "")
        assert await voice_worker.process_one(send, OWNER) is False
        assert send.sent == [f"Не смогла выполнить поручение: „{TEST_INSTRUCTION}“."]

    @pytest.mark.asyncio
    async def test_stale_command_with_attempts_left_is_retried(self, sqlite_db):
        await accept_voice_command(_cmd(), x_internal_secret=SECRET)
        await _set_row(sqlite_db, status="processing", attempts=1,
                       updated_at=datetime(2026, 1, 1))
        assert await voice_commands.revive_stale() == 0
        [row] = await _rows(sqlite_db, VoiceCommandRow)
        assert row.status == "pending"

    @pytest.mark.asyncio
    async def test_answer_already_sent_is_not_sent_again_after_restart(self, sqlite_db):
        """Ответ ушёл, процесс упал до finish — перезапуск только закрывает строку."""
        await accept_voice_command(_cmd(), x_internal_secret=SECRET)
        await _set_row(sqlite_db, acked_at=datetime(2026, 9, 30),
                       answered_at=datetime(2026, 9, 30))
        send = _Send()
        ask = AsyncMock(return_value=_answer())
        with patch.object(voice_worker, "ask_brain", ask), \
             patch.object(voice_worker, "save_event", AsyncMock()):
            assert await voice_worker.process_one(send, OWNER) is True
        assert send.sent == [] and ask.await_count == 0
        [row] = await _rows(sqlite_db, VoiceCommandRow)
        assert row.status == "done"

    @pytest.mark.asyncio
    async def test_no_owner_means_no_work(self):
        claim = AsyncMock()
        with patch.object(voice_worker, "claim_command", claim):
            await voice_worker.run_forever(_Send(), 0)
        claim.assert_not_awaited()


class TestOnlyOwner:
    @pytest.mark.asyncio
    async def test_bot_sends_first_only_to_owner(self):
        from bot_telegram import bot as bot_mod
        sent = AsyncMock(return_value=type("M", (), {"message_id": 7})())
        with patch.object(bot_mod.bot, "send_message", sent):
            assert await bot_mod.send_to_owner("<b>x</b>", "x") == 7
        assert sent.await_args.args[0] == OWNER

    @pytest.mark.asyncio
    async def test_bot_refuses_to_send_without_owner(self):
        from bot_telegram import bot as bot_mod
        sent = AsyncMock()
        with patch.object(bot_mod, "OWNER_ID", 0), \
             patch.object(bot_mod.bot, "send_message", sent):
            with pytest.raises(RuntimeError):
                await bot_mod.send_to_owner("x", "x")
        sent.assert_not_awaited()
