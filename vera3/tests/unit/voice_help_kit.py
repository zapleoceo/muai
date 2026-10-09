"""Общее для тестов срочной просьбы: команда, отправщики, чтение комнаты.

Тесты ходят только в комнату `voice-test` (фикстура `help_room`): боевая
`main` в них не появляется даже на SQLite-фикстуре.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bot_telegram.brain import BrainAnswer
from gateway.voice_command import VoiceCommand
from sqlalchemy import select
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomMessageRow, RoomTaskRow
from vera_shared.db.models_voice import VoiceCommandRow

SECRET = "test-internal-secret"
OWNER = 169510539
ROOM = "voice-test"
INSTRUCTION = "упал деплой бота, посмотри логи"


def help_cmd(command_id: str = "vc-help1", *, confidence: float | None = 0.95,
             instruction: str = INSTRUCTION, kind: str = "command",
             doubts: list[str] | None = None, app: str = "Code.exe",
             window_title: str = "myAI — vera3", **extra: Any) -> VoiceCommand:
    return VoiceCommand(
        command_id=command_id, kind=kind, instruction=instruction,
        spoken_at=datetime.now(timezone.utc), app=app, window_title=window_title,
        session_id="s-9", start=5.0, end=9.0,
        fragment=[{"start": 5.0, "end": 9.0,
                   "text": f"Вера, мне нужна помощь, {instruction}"}],
        confidence=confidence, guard="own", doubts=doubts or [], **extra)


def answer() -> BrainAnswer:
    return BrainAnswer(raw="Смотрю логи.", provider="p", cost_usd=0.0,
                       n_results=0, n_history=0)


class Recorder:
    """И `Send(html, plain)`, и `Ask(html, plain, command_id)`."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.asked: list[str] = []

    async def __call__(self, html: str, plain: str) -> int:
        self.sent.append(plain)
        return len(self.sent)

    async def ask(self, html: str, plain: str, command_id: str) -> int:
        self.asked.append(command_id)
        self.sent.append(plain)
        return len(self.sent)


async def tasks_in(room: str) -> list[RoomTaskRow]:
    async with get_session() as s:
        return list((await s.execute(
            select(RoomTaskRow).where(RoomTaskRow.room == room))).scalars())


async def messages_in(room: str) -> list[RoomMessageRow]:
    async with get_session() as s:
        return list((await s.execute(
            select(RoomMessageRow).where(RoomMessageRow.room == room)
            .order_by(RoomMessageRow.id))).scalars())


async def queue_row(command_id: str) -> VoiceCommandRow:
    async with get_session() as s:
        return await s.get(VoiceCommandRow, command_id)
