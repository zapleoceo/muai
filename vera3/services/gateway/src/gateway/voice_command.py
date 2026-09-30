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

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field
from vera_shared.voice_commands import create_command

from gateway.auth import check_internal_secret

log = logging.getLogger(__name__)
router = APIRouter()


class VoiceCommand(BaseModel):
    command_id: str = Field(min_length=1, max_length=64)
    instruction: str = Field(min_length=1, max_length=2000)
    spoken_at: datetime
    app: str | None = None
    window_title: str | None = None


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
        app=body.app, window_title=body.window_title)
    # Текст поручения — только DEBUG: это содержимое сообщения владельца.
    log.info("voice-command: %s -> event=%s%s (%d симв.)", body.command_id,
             event_id, ", повтор" if deduped else "", len(body.instruction))
    log.debug("voice-command %s: %s", body.command_id, body.instruction)
    return VoiceCommandResult(ok=True, event_id=event_id, deduped=deduped)
