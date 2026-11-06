"""Track accepted help work and deliver executor results with actual receipts.

Room completion is readiness, not Telegram delivery. Unknown sends are never
replayed; legacy competing finals and unavailable evidence require review.
Queue state and final receipt are committed atomically by the shared repository."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from html import escape

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.db.models_voice import VoiceCommandRow
from vera_shared.redact import redact_secrets
from vera_shared.voice_commands import GUARD_VERSION
from vera_shared.voice_help.delivery_claim import classify_send_error, settle
from vera_shared.voice_help.fact_route import external_facts, fresh_result
from vera_shared.voice_help.hold import is_confirmed, lift_hold
from vera_shared.voice_help.policy import help_room, source_uncertain
from vera_shared.voice_help.queue_state import (
    mark_escalated,
    mark_reminded,
    mark_taken,
    watched_help,
)
from vera_shared.voice_help.result_delivery import (
    cancel_help,
    reserve_result,
    review_result,
    stale_result_claim,
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
    FINAL_TASK_STATUSES,
    REMIND,
    TAKEN,
    next_step,
    task_holder,
)

from bot_telegram.formatting import TELEGRAM_MAX_CHARS

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


async def _result_unknown(command_id: str, why: str, now: datetime) -> None:
    """Исход отправки неизвестен: не повторяем, строка не закрывается."""
    await settle(command_id, "executor", "unknown")
    await move(command_id, (PENDING,), UNKNOWN, now)
    log.warning("help-worker: результат %s — исход неизвестен (%s), автоповтора нет",
                command_id, why)


async def deliver_result(send: Send, row: VoiceCommandRow, now: datetime) -> bool:
    """Результат исполнителя → владельцу. → True, если строку можно закрывать.

    Двухфазно (см. result_state): намерение пишется ДО отправки; квитанция — после.
    Нет текста (или он старше выкатки) — слать нечего, закрываем как раньше.
    """
    source = row.source or {}
    uncertain = (source.get("guard_version") != GUARD_VERSION or source.get("guard") != "own"
                 or source_uncertain(row.confidence, source.get("doubts") or []))
    if uncertain:
        async with get_session() as session:
            task = await session.get(RoomTaskRow, (help_room(), row.task_id))
        if task is None or not is_confirmed(task.refs):
            await review_result(row.command_id, "source_unverified", now)
            return False
    since = delivery_since()
    if since is None or row.created_at < since:
        await review_result(row.command_id, "delivery_cutoff", now)
        return False
    found = await executor_result(help_room(), row.task_id)
    if found is None:
        await review_result(row.command_id, "missing_final_text", now)
        return False
    if external_facts(row.instruction):
        checked = fresh_result(found[1], now)
        if checked is None:
            await review_result(row.command_id, "unverified_external_result", now)
            return False
        found = (found[0], checked)
    errors = result_errors(row)
    if errors >= RESULT_MAX_ERRORS:
        return False
    held = await reserve_result(row.command_id, now)
    if held is not None:
        if held.get("owner") == "brain":
            await review_result(row.command_id, "legacy_brain_final", now)
        return False
    text = result_text(row.task_id, *found)
    try:
        message_id = await send(escape(text, quote=False), text)
    except Exception as e:
        # send_to_owner без OWNER_TELEGRAM_ID бросает — это и есть fail-closed.
        if classify_send_error(e) == "error":
            await settle(row.command_id, "executor", "error")
            log.warning("help-worker: результат %s не доставлен (%s), ошибка %d/%d",
                        row.command_id, type(e).__name__, errors + 1, RESULT_MAX_ERRORS)
            await record_error(row.command_id, errors + 1, now)
        else:
            await _result_unknown(row.command_id, type(e).__name__, now)
        return False
    if type(message_id) is not int or message_id <= 0:
        await _result_unknown(row.command_id, "no message_id", now)
        return False
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
    delivery = (row.source or {}).get("delivery", {})
    if delivery.get("owner") == "brain" and delivery.get("state") != "error":
        await review_result(row.command_id, "legacy_brain_final", now)
        return None
    if row.help_state == PENDING:
        if not stale_result_claim(row, now):
            return None
        await _result_unknown(row.command_id, "stale reservation", now)
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
    elif step == ESCALATE:
        await escalate(task_id=row.task_id)
        await say(send, ESCALATED_TEXT)
        await mark_escalated(row.command_id, now)
    elif step == REMIND:
        await say(send, REMIND_TEXT)
        await mark_reminded(row.command_id, now)
    elif step == CLOSED:
        if status == "done":
            if not await deliver_result(send, row, now):
                return None
        elif status == "cancelled":
            await cancel_help(row.command_id, row.help_state, now)
        else:
            await review_result(row.command_id, "execution_ended_without_delivery", now)
            return None
    return step


ASSIGNED, OFFERED, ESCALATING, FAILED, EXECUTOR_FINAL = (
    "assigned", "offered", "escalating", "failed", "executor_final")
NOTICE_KEY = "notice"


def assigned_text(agent: str) -> str:
    return f"Мозг не ответил; задача у {agent}, в работе."


def offered_text(agent: str) -> str:
    return f"Мозг не ответил. Передаю задачу {agent}, жду принятия."


NO_TAKER_TEXT = "Мозг не ответил; никто не взял задачу, эскалировала dot."


async def failure_route(row: VoiceCommandRow) -> tuple[str, str]:
    """Что сказать владельцу, когда мозг не ответил. → (маршрут, агент).

    Честное «не смогла» — только когда задачи нет, она закрыта/заблокирована или
    держится без подтверждённого источника (полномочий нет).
    """
    if not row.task_id:
        return FAILED, ""
    holder, status = await task_holder(help_room(), row.task_id)
    if status == "done" or (row.source or {}).get("delivery", {}).get("owner") == "executor" \
            or row.help_state in (PENDING, UNKNOWN, ERROR):
        # исполнитель довёл задачу: его финал зарезервирован, в пути, доставлен или
        # неясен — «не смогла» было бы ложью
        return EXECUTOR_FINAL, ""
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
        return OFFERED, task.pending_handoff_to
    return ESCALATING, ""
