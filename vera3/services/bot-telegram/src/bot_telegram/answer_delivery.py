"""Conservative delivery for non-help memory answers.

Help finals belong exclusively to the Room executor, including before acceptance."""
from __future__ import annotations

import logging

from vera_shared.db.models_voice import VoiceCommandRow
from vera_shared.timeutil import utc_naive_now
from vera_shared.voice_help.delivery_claim import (
    claim,
    classify_send_error,
    settle,
)

from bot_telegram.brain import BrainAnswer
from bot_telegram.formatting import format_reply, plain_fallback

log = logging.getLogger(__name__)

async def _settle_failure(command_id: str, key: str, exc: BaseException | None) -> bool:
    """→ True, если сбой точный и повтор безопасен."""
    definite = exc is not None and classify_send_error(exc) == "error"
    await settle(command_id, "brain", "error" if definite else "unknown", key=key)
    return definite


async def deliver_answer(send, row: VoiceCommandRow, answer: BrainAnswer) -> int | None:
    """→ message_id, если финальный ответ ушёл и это подтверждено; иначе None."""
    if row.help_state:
        # The executor owns help finals even before accepting a handoff.
        return None
    lost = await claim(row.command_id, "brain", utc_naive_now())
    if lost is not None:
        log.info("voice-worker: %s — заявка у %s (%s), ответ мозга не шлю",
                 row.command_id, lost["owner"], lost["state"])
        return None
    try:
        msg_id = await send(
            format_reply(answer.raw, answer.provider, answer.cost_usd,
                         answer.n_results, answer.n_history),
            plain_fallback(answer.raw, answer.provider))
    except Exception as e:
        if await _settle_failure(row.command_id, "delivery", e):
            raise
        log.warning("voice-worker: ответ %s — исход неизвестен (%s), автоповтора нет",
                    row.command_id, type(e).__name__)
        return None
    if not msg_id:
        await _settle_failure(row.command_id, "delivery", None)
        log.warning("voice-worker: ответ %s без message_id — исход неизвестен", row.command_id)
        return None
    await settle(row.command_id, "brain", "sent", msg_id)
    return msg_id
