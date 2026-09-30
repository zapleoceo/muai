"""Голосовое поручение: приём в шлюзе → очередь → ответ владельцу в Telegram.

Закрепляем: без секрета шлюз не пишет ничего; ретрай слушателя не задваивает
поручение; бот отвечает двумя сообщениями (подтверждение и результат) тем же
путём, что на текст; адресат — только владелец, а без него воркер молчит.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from gateway.voice_command import VoiceCommand, accept_voice_command
from pydantic import ValidationError
from sqlalchemy import select
from vera_shared.db.models import EventRow
from vera_shared.db.models_voice import VoiceCommandRow

from bot_telegram import voice_worker
from bot_telegram.brain import BrainAnswer

SECRET = "test-internal-secret"
OWNER = 169510539
TEST_INSTRUCTION = "срочно напиши мне что-то в телеграм"


def _cmd(command_id: str = "vc-1", instruction: str = TEST_INSTRUCTION) -> VoiceCommand:
    return VoiceCommand(command_id=command_id, instruction=instruction,
                        spoken_at=datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc),
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
    async def test_failed_answer_is_retried_without_second_ack(self, sqlite_db):
        await accept_voice_command(_cmd(), x_internal_secret=SECRET)
        send = _Send()
        ask = AsyncMock(side_effect=[RuntimeError("search down"), _answer()])
        with patch.object(voice_worker, "ask_brain", ask), \
             patch.object(voice_worker, "save_event", AsyncMock()):
            await voice_worker.process_one(send, OWNER)
            [row] = await _rows(sqlite_db, VoiceCommandRow)
            assert row.status == "pending"
            await voice_worker.process_one(send, OWNER)
        assert sum(m.startswith("Услышала") for m in send.sent) == 1
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
