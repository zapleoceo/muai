"""Срочная просьба голосом: «Открыл задачу», «Взял: …» и эскалация dot.

Живёт в том же цикле, что `voice_worker`: второй шины нет, состояние — в
строке очереди (`vera_shared.voice_help.queue_state`): до claim она в
`status=opened`, на claim/отмене — `release_held`. Ответ владельца на вопрос
неподтверждённой задачи снимает удержание (`voice_help.hold.lift_hold`). Каждый шаг — сначала
сообщение владельцу, потом отметка: упади бот между ними, следующий проход
повторит сообщение (at-least-once, как у ответа на поручение).

Кнопок нет (решение владельца 11.10.2026): владельцу уходит только
уведомление, задача открывается сразу.

Часы приходят аргументом `now` — тесты подставляют свои.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from html import escape

from vera_shared.db.models_voice import VoiceCommandRow
from vera_shared.redact import redact_secrets
from vera_shared.voice_help.hold import lift_hold
from vera_shared.voice_help.policy import help_room
from vera_shared.voice_help.queue_state import (
    mark_closed,
    mark_escalated,
    mark_reminded,
    mark_taken,
    release_held,
    watched_help,
)
from vera_shared.voice_help.room_intake import escalate, normal_action
from vera_shared.voice_help.tracking import (
    CLOSED,
    ESCALATE,
    REMIND,
    TAKEN,
    next_step,
    task_holder,
)

log = logging.getLogger(__name__)

Send = Callable[[str, str], Awaitable[int]]

REPROMPT_TEXT = "Не расслышала поручение, повтори."


def opened_text(task_id: str, instruction: str, *, uncertain: bool = False) -> str:
    prefix = "Источник не подтверждён. " if uncertain else ""
    return f"{prefix}Открыл задачу {task_id}: {instruction}"


def taken_text(agent: str) -> str:
    return f"Взял: {agent}"


ESCALATED_TEXT = "Пока никто не взял, эскалировала dot."
REMIND_TEXT = "Срочную просьбу всё ещё никто не взял (20 мин). Посмотри комнату."


async def say(send: Send, text: str) -> None:
    """Распознанная речь в тексте: «<» в ней сломал бы HTML."""
    await send(escape(text, quote=False), text)


async def track_help(send: Send, now: datetime) -> int:
    """Один проход по открытым срочным задачам. → сколько шагов сделано."""
    steps = 0
    for row in await watched_help():
        try:
            step = await _track_one(send, row, now)
        except Exception as e:
            # Одна задача со сбоем не останавливает слежение за остальными.
            log.warning("help-worker: %s не обработан (%s) — повторю",
                        row.command_id, type(e).__name__)
            continue
        if step is None:
            continue
        log.info("help-worker: %s → %s", row.command_id, step)
        steps += 1
    return steps


async def _track_one(send: Send, row: VoiceCommandRow, now: datetime) -> str | None:
    if row.instruction and await lift_hold(
            help_room(), row.task_id, normal_action(redact_secrets(row.instruction))):
        log.info("help-worker: %s источник подтверждён владельцем", row.command_id)
    holder, status = await task_holder(help_room(), row.task_id)
    elapsed = now - (row.task_opened_at or now)
    step = next_step(row.help_state, elapsed, holder, status)
    if step == TAKEN and holder:
        await say(send, taken_text(holder))
        await mark_taken(row.command_id, holder, now)
        await release_held(row.command_id, now)
    elif step == ESCALATE:
        await escalate(task_id=row.task_id)
        await say(send, ESCALATED_TEXT)
        await mark_escalated(row.command_id, now)
    elif step == REMIND:
        await say(send, REMIND_TEXT)
        await mark_reminded(row.command_id, now)
    elif step == CLOSED:
        await mark_closed(row.command_id, now)
        await release_held(row.command_id, now)
    return step
