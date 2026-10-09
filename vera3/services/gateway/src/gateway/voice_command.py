"""POST /v1/voice/command — голосовое поручение владельца с ноутбука.

Слушатель ловит кодовую фразу («Вера, мне нужна помощь, …») на дорожке
микрофона, отсекает эхо собеседника и присылает сюда только текст поручения.
Та же аутентификация, что у сессий: `X-Internal-Secret`, fail-closed.

Шлюз ничего не исполняет сам: пишет событие и строку очереди, а отвечает
владельцу бот (`bot_telegram.voice_worker`). Так ответ идёт ровно тем путём,
что и на текстовое сообщение, а адресат ответа задан в боте и только там.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field, model_validator
from vera_shared.voice_commands import create_command

from gateway.auth import check_internal_secret

log = logging.getLogger(__name__)
router = APIRouter()


class FragmentLine(BaseModel):
    start: float
    end: float
    text: str = Field(max_length=2000)


class VoiceCommand(BaseModel):
    """Поручение или «переспроси» (`kind="reprompt"`, поручение пустое).

    `fragment` — реплика с фразой и не больше двух продолжений: весь разговор
    шлюз не принимает и по длине. Поля уверенности — от нового слушателя; без
    них поручение идёт старым путём, только ответом мозга.
    """

    command_id: str = Field(min_length=1, max_length=64)
    kind: Literal["command", "reprompt"] = "command"
    instruction: str = Field(default="", max_length=2000)
    spoken_at: datetime
    app: str | None = None
    window_title: str | None = None
    session_id: str | None = Field(default=None, max_length=128)
    start: float | None = None
    end: float | None = None
    fragment: list[FragmentLine] = Field(default_factory=list, max_length=4)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    guard: Literal["own"] | None = None
    doubts: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def _instruction_for_command(self) -> VoiceCommand:
        if self.kind == "command" and not self.instruction.strip():
            raise ValueError("instruction is required for kind=command")
        return self


class VoiceCommandResult(BaseModel):
    ok: bool
    event_id: int | None
    deduped: bool = False


@router.post("/v1/voice/command", response_model=VoiceCommandResult)
async def accept_voice_command(
    body: VoiceCommand,
    x_internal_secret: str | None = Header(default=None),
) -> VoiceCommandResult:
    check_internal_secret(x_internal_secret)
    spoken = body.spoken_at.astimezone(timezone.utc).replace(tzinfo=None)
    event_id, deduped = await create_command(
        body.command_id, body.instruction.strip(), spoken,
        app=body.app, window_title=body.window_title, kind=body.kind,
        confidence=body.confidence, doubts=body.doubts,
        source={"session_id": body.session_id, "start": body.start, "end": body.end},
        fragment=[line.model_dump() for line in body.fragment])
    # Текст поручения — только DEBUG: это содержимое сообщения владельца.
    log.info("voice-command: %s (%s, уверенность %s) -> event=%s%s (%d симв.)",
             body.command_id, body.kind, body.confidence, event_id,
             ", повтор" if deduped else "", len(body.instruction))
    log.debug("voice-command %s: %s", body.command_id, body.instruction)
    return VoiceCommandResult(ok=True, event_id=event_id, deduped=deduped)
