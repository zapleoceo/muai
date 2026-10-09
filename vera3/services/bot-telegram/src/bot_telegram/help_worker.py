"""Срочная просьба голосом: «Это ты сказал?», «Взял: …» и эскалация dot.

Живёт в том же цикле, что `voice_worker`: второй шины нет, состояние — в
строке очереди (`vera_shared.voice_help.queue_state`). Каждый шаг — сначала
сообщение владельцу, потом отметка: упади бот между ними, следующий проход
повторит сообщение (at-least-once, как у ответа на поручение).

Часы приходят аргументом `now` — тесты подставляют свои.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from html import escape

from vera_shared.voice_help.policy import help_room
from vera_shared.voice_help.queue_state import (
    expired_asks,
    mark_asked,
    mark_closed,
    mark_escalated,
    mark_expired,
    mark_reminded,
    mark_taken,
    watched_help,
)
from vera_shared.voice_help.room_intake import escalate
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
#: `Ask(html, plain, command_id)` — сообщение с кнопками «Да / Нет».
Ask = Callable[[str, str, str], Awaitable[int]]

REPROMPT_TEXT = "Не расслышала поручение, повтори."


def confirm_text(instruction: str) -> str:
    return f"Это ты сказал: „{instruction}“?"


def opened_ack_text(instruction: str) -> str:
    return f"Услышала: „{instruction}“. Завела срочную задачу, жду, кто возьмёт."


def taken_text(agent: str) -> str:
    return f"Взял: {agent}"


ESCALATED_TEXT = "Пока никто не взял, эскалировала dot."
REMIND_TEXT = "Срочную просьбу всё ещё никто не взял (20 мин). Посмотри комнату."
EXPIRED_TEXT = "Не дождалась ответа на «Это ты сказал?» — отменила, ничего не делаю."


async def _say(send: Send, text: str) -> None:
    await send(escape(text, quote=False), text)


async def ask_confirmation(ask: Ask, command_id: str, instruction: str,
                           now: datetime) -> None:
    text = confirm_text(instruction)
    await ask(escape(text, quote=False), text, command_id)
    await mark_asked(command_id, now)


async def expire_confirmations(send: Send, now: datetime) -> int:
    """Отменить вопросы без ответа дольше 10 мин. → сколько отменено."""
    done = 0
    for command_id in await expired_asks(now):
        if await mark_expired(command_id, now):
            await _say(send, EXPIRED_TEXT)
            done += 1
    return done


async def track_help(send: Send, now: datetime) -> int:
    """Один проход по открытым срочным задачам. → сколько шагов сделано."""
    steps = 0
    for row in await watched_help():
        holder, status = await task_holder(help_room(), row.task_id)
        elapsed = now - (row.task_opened_at or now)
        step = next_step(row.help_state, elapsed, holder, status)
        if step == TAKEN and holder:
            await _say(send, taken_text(holder))
            await mark_taken(row.command_id, holder, now)
        elif step == ESCALATE:
            await escalate(task_id=row.task_id)
            await _say(send, ESCALATED_TEXT)
            await mark_escalated(row.command_id, now)
        elif step == REMIND:
            await _say(send, REMIND_TEXT)
            await mark_reminded(row.command_id, now)
        elif step == CLOSED:
            await mark_closed(row.command_id, now)
        else:
            continue
        log.info("help-worker: %s → %s", row.command_id, step)
        steps += 1
    return steps
