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

from bot_telegram.formatting import TELEGRAM_MAX_CHARS
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.db.models_voice import VoiceCommandRow
from vera_shared.redact import redact_secrets
from vera_shared.voice_help.delivery_claim import claim, settle
from vera_shared.voice_help.hold import is_confirmed, lift_hold
from vera_shared.voice_help.policy import help_room, source_uncertain
from vera_shared.voice_help.queue_state import (
    mark_closed,
    mark_escalated,
    mark_reminded,
    mark_taken,
    release_held,
    watched_help,
)
from vera_shared.voice_help.result_state import (
    ERROR,
    PENDING,
    RESULT_MAX_ERRORS,
    UNKNOWN,
    delivery_since,
    executor_result,
    move,
    record_error,
    record_receipt,
    result_errors,
)
from vera_shared.voice_help.room_intake import escalate, normal_action
from vera_shared.voice_help.tracking import (
    CLOSED,
    ESCALATE,
    REMIND,
    FINAL_TASK_STATUSES,
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


def result_text(task_id: str, agent: str, text: str) -> str:
    head = f"Результат по задаче {task_id} ({agent}): "
    body = redact_secrets(text)
    room_left = TELEGRAM_MAX_CHARS - len(head)
    return head + (body if len(body) <= room_left else body[:room_left - 1] + "…")


async def deliver_result(send: Send, row: VoiceCommandRow, now: datetime) -> bool:
    """Результат исполнителя → владельцу. → True, если строку можно закрывать.

    Двухфазно (см. result_state): намерение пишется ДО отправки; квитанция — после.
    Нет текста (или он старше выкатки) — слать нечего, закрываем как раньше.
    """
    since = delivery_since()
    if since is None or row.created_at < since:
        return True  # выключено или команда старше выкатки: закрываем как раньше
    found = await executor_result(help_room(), row.task_id)
    if found is None:
        return True
    errors = result_errors(row)
    if errors >= RESULT_MAX_ERRORS or not await move(
            row.command_id, (row.help_state,), PENDING, now):
        return False
    held = await claim(row.command_id, "executor", now)
    if held is not None:
        # Ответ мозга (или чужая заявка) уже считается доставкой: второго сообщения нет.
        log.info("help-worker: %s — заявка у %s (%s), результат не шлю",
                 row.command_id, held["owner"], held["state"])
        return held["owner"] == "brain" and held["state"] == "sent"
    text = result_text(row.task_id, *found)
    try:
        message_id = await send(escape(text, quote=False), text)
    except Exception as e:
        await settle(row.command_id, "executor", "error")
        # send_to_owner без OWNER_TELEGRAM_ID бросает — это и есть fail-closed.
        log.warning("help-worker: результат %s не отправлен (%s), ошибка %d/%d",
                    row.command_id, type(e).__name__, errors + 1, RESULT_MAX_ERRORS)
        await record_error(row.command_id, errors + 1, now)
        return False
    if not message_id:
        await settle(row.command_id, "executor", "error")
        log.warning("help-worker: результат %s без message_id, ошибка %d/%d",
                    row.command_id, errors + 1, RESULT_MAX_ERRORS)
        await record_error(row.command_id, errors + 1, now)
        return False
    await settle(row.command_id, "executor", "sent", message_id)
    return await record_receipt(row.command_id, message_id, now)


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
    if row.help_state == PENDING:
        # Намерение есть, квитанции нет: исход отправки неизвестен — не повторяем.
        await move(row.command_id, (PENDING,), UNKNOWN, now)
        log.warning("help-worker: %s — результат мог уйти, квитанции нет; "
                    "автоповтора нет (result_unknown)", row.command_id)
        return UNKNOWN
    if row.instruction and await lift_hold(
            help_room(), row.task_id, normal_action(redact_secrets(row.instruction))):
        log.info("help-worker: %s источник подтверждён владельцем", row.command_id)
    holder, status = await task_holder(help_room(), row.task_id)
    elapsed = now - (row.task_opened_at or now)
    step = next_step(row.help_state, elapsed, holder, status)
    if step == TAKEN and row.help_state in ("taken", ERROR):
        return None  # «Взял» уже сказано; строку держим ради результата
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
        if status == "done" and not await deliver_result(send, row, now):
            return None
        await mark_closed(row.command_id, now)
        await release_held(row.command_id, now)
    return step


ASSIGNED, ESCALATING, FAILED = "assigned", "escalating", "failed"
NOTICE_KEY = "notice"


def assigned_text(agent: str) -> str:
    return f"Мозг не ответил; задача передана {agent}, в работе."


NO_TAKER_TEXT = "Мозг не ответил; никто не взял задачу, эскалировала dot."


async def failure_route(row: VoiceCommandRow) -> tuple[str, str]:
    """Что сказать владельцу, когда мозг не ответил. → (маршрут, агент).

    Честное «не смогла» — только когда задачи нет, она закрыта/заблокирована или
    держится без подтверждённого источника (полномочий нет).
    """
    if not row.task_id:
        return FAILED, ""
    holder, status = await task_holder(help_room(), row.task_id)
    if status is None or status in FINAL_TASK_STATUSES or status == "blocked":
        return FAILED, ""
    async with get_session() as s:
        task = await s.get(RoomTaskRow, (help_room(), row.task_id))
    source = row.source or {}
    uncertain = (source.get("guard") != "own"
                 or source_uncertain(row.confidence, source.get("doubts") or []))
    if task is None or (uncertain and not is_confirmed(task.refs)):
        return FAILED, ""
    if holder:
        return ASSIGNED, holder
    if task.pending_handoff_to:
        return ASSIGNED, task.pending_handoff_to
    return ESCALATING, ""
