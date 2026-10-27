"""Финальный ответ мозга владельцу — через общую заявку на финал.

Политика «одна срочная задача — одно финальное сообщение»: после `handoff_accept`
финал за исполнителем, мозг шлёт только короткий статус (заявка `progress`, финал не
трогает). До приёма мозг заканчивает через заявку `delivery`; если её уже занял
исполнитель (или прошлая отправка с неясным исходом) — не шлёт.
"""
from __future__ import annotations

import logging
from html import escape

from vera_shared.db.models_voice import VoiceCommandRow
from vera_shared.timeutil import utc_naive_now
from vera_shared.voice_help.delivery_claim import claim, classify_send_error, settle
from vera_shared.voice_help.policy import help_room, help_task_id
from vera_shared.voice_help.tracking import task_holder

from bot_telegram.brain import BrainAnswer
from bot_telegram.formatting import format_reply, plain_fallback

log = logging.getLogger(__name__)

PROGRESS_KEY = "progress"


def status_only_text(agent: str) -> str:
    return f"Мозг ответил, задачу ведёт {agent}."


async def _settle_failure(command_id: str, key: str, exc: BaseException | None) -> bool:
    """→ True, если сбой точный и повтор безопасен."""
    definite = exc is not None and classify_send_error(exc) == "error"
    await settle(command_id, "brain", "error" if definite else "unknown", key=key)
    return definite


async def _status_only(send, row: VoiceCommandRow, agent: str) -> None:
    lost = await claim(row.command_id, "brain", utc_naive_now(), PROGRESS_KEY)
    if lost is not None:
        log.info("voice-worker: %s — статус уже в заявке (%s)", row.command_id, lost["state"])
        return
    text = status_only_text(agent)
    try:
        msg_id = await send(escape(text, quote=False), text)
    except Exception as e:
        if await _settle_failure(row.command_id, PROGRESS_KEY, e):
            raise
        log.warning("voice-worker: статус %s — исход неизвестен, автоповтора нет", row.command_id)
        return
    if not msg_id:
        await _settle_failure(row.command_id, PROGRESS_KEY, None)
        log.warning("voice-worker: статус %s без message_id — исход неизвестен", row.command_id)
        return
    await settle(row.command_id, "brain", "sent", msg_id, key=PROGRESS_KEY)


async def deliver_answer(send, row: VoiceCommandRow, answer: BrainAnswer) -> int | None:
    """→ message_id, если финальный ответ ушёл и это подтверждено; иначе None."""
    if row.help_state:  # строка могла открыть задачу только что — task_id в ней ещё пуст
        holder, _ = await task_holder(help_room(), row.task_id or help_task_id(row.command_id))
        if holder:
            await _status_only(send, row, holder)
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
