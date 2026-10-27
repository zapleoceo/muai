"""Голосовые поручения: бот сам пишет владельцу первым.

Поручение приходит из очереди (`vera_shared.voice_commands`), его кладёт
шлюз. Сначала — «Услышала: „…“. Делаю.», чтобы владелец посреди звонка знал,
что его поняли; потом — ответ мозга тем же путём, что на текстовое сообщение
(`brain.ask_brain`).

Границы первой версии, и они здесь, а не в слушателе или шлюзе:
- адресат один — владелец. `Send` уже привязан к OWNER_TELEGRAM_ID, чата в
  этом модуле нет вовсе; без владельца воркер не берёт из очереди ничего;
- действий с внешним эффектом нет: поручение — это вопрос мозгу, и
  единственный побочный эффект — ответ владельцу.

Опрос раз в POLL_S, а не LISTEN/NOTIFY: таблица крошечная, запрос по индексу,
а уведомления потребовали бы отдельного постоянного соединения мимо пула —
ради выигрыша в две секунды на ответе, которому и так ждать поиска.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from html import escape

from vera_shared.db.models_voice import VoiceCommandRow
from vera_shared.timeutil import utc_naive_now
from vera_shared.voice_commands import (
    GUARD_VERSION,
    claim_command,
    fail_command,
    finish_command,
    get_command,
    mark_acked,
    mark_answered,
    mark_notified,
    pending_notifications,
    revive_stale,
)
from vera_shared.voice_help.delivery_claim import claim, classify_send_error, settle
from vera_shared.voice_help.policy import source_uncertain
from vera_shared.voice_help.queue_state import (
    WATCHED,
    hold_command,
    mark_escalated,
    mark_opened,
)
from vera_shared.voice_help.room_intake import escalate, open_help_task

from bot_telegram.answer_delivery import deliver_answer
from bot_telegram.brain import BrainError, ask_brain, save_event
from bot_telegram.help_worker import (
    ASSIGNED,
    ESCALATING,
    EXECUTOR_FINAL,
    FAILED,
    NO_TAKER_TEXT,
    NOTICE_KEY,
    OFFERED,
    REPROMPT_TEXT,
    assigned_text,
    failure_route,
    offered_text,
    opened_text,
    say,
    track_help,
)

log = logging.getLogger(__name__)

POLL_S = 2.0
#: `Send(html, plain)` → id отправленного сообщения. plain — запасной текст на
#: случай, если Telegram отклонит HTML (см. formatting.plain_fallback).
Send = Callable[[str, str], Awaitable[int]]

#: Строки, по которым открывается срочная задача. `confirm` — от версии с
#: кнопкой «Это ты сказал?»: такие строки теперь открываются так же.
HELP_TO_OPEN = ("ready", "confirm", "opened")


#: Поручение старше этого не исполняется: «срочно напиши» через сутки после
#: звонка (шлюз лежал, ноутбук был без сети) — уже не то, о чём просили.
#: Владелец узнаёт об этом сообщением и может сказать ещё раз.
MAX_AGE = timedelta(minutes=30)


def stale_text(instruction: str, age: timedelta) -> str:
    minutes = int(age.total_seconds() // 60)
    return (f"Поручение дошло с опозданием ({minutes} мин), не выполняю: "
            f"„{instruction}“. Скажи ещё раз, если ещё нужно.")


def ack_text(instruction: str) -> str:
    return f"Услышала: „{instruction}“. Делаю."


def failed_text(instruction: str) -> str:
    if instruction:
        return f"Не смогла выполнить поручение: „{instruction}“."
    return "Не смогла выполнить голосовое поручение."


def _ms(since: datetime | None, now: datetime) -> str:
    return "?" if since is None else f"{(now - since).total_seconds() * 1000:.0f}"


async def open_and_notify(send: Send, row: VoiceCommandRow, now: datetime) -> bool:
    """Открыть задачу и сообщить владельцу. → неподтверждён ли источник.

    Неподтверждённый источник — только задача с пометкой и уведомление:
    ответ мозга не запускается, ничего не исполняется.
    """
    source = row.source or {}
    uncertain = (source.get("guard_version") != GUARD_VERSION
                 or source.get("guard") != "own"
                 or source_uncertain(row.confidence, source.get("doubts") or []))
    task_id = await open_help_task(
        command_id=row.command_id, event_id=row.event_id,
        instruction=row.instruction, source=row.source, uncertain=uncertain)
    if row.help_state != "opened":
        await mark_opened(row.command_id, task_id, now)
    opened = utc_naive_now()
    if row.acked_at is None:
        await say(send, opened_text(task_id, row.instruction, uncertain=uncertain))
        await mark_acked(row.command_id)
    log.info("voice-worker: %s очередь→задача %s мс, →уведомление %s мс%s",
             row.command_id, _ms(row.created_at, opened),
             _ms(row.created_at, utc_naive_now()),
             " (источник не подтверждён)" if uncertain else "")
    return uncertain


async def process_one(send: Send, owner_id: int) -> bool:
    """Ответить на одно поручение. False — очередь пуста.

    Гарантия — ответ не теряется и обычно приходит один раз. Дубль возможен в
    одном узком окне: Telegram принял сообщение, а `mark_answered` не успел
    записаться (процесс убит в эти миллисекунды, упала БД) или ответ Telegram
    потерялся в сети. Тогда ретрай ответит повторно — at-least-once: потерять
    ответ хуже, чем прислать его дважды. То же окно у «Услышала» (`acked_at`).
    """
    row = await claim_command()
    if row is None:
        return False
    try:
        now = utc_naive_now()
        held = row.help_state in WATCHED
        age = now - row.spoken_at
        if row.answered_at is None and row.acked_at is None and age > MAX_AGE:
            text = stale_text(row.instruction, age)
            await send(escape(text, quote=False), text)
            await mark_answered(row.command_id)
        elif row.kind == "reprompt":
            if row.answered_at is None:
                await send(REPROMPT_TEXT, REPROMPT_TEXT)
                await mark_answered(row.command_id)
        elif row.answered_at is None:
            if row.help_state in HELP_TO_OPEN:
                held = True
                if await open_and_notify(send, row, now):
                    await hold_command(row.command_id, now)
                    return True
            elif row.acked_at is None:
                await say(send, ack_text(row.instruction))
                await mark_acked(row.command_id)
            answer = await ask_brain(row.instruction, owner_id, owner_id)
            msg_id = await deliver_answer(send, row, answer)
            if msg_id:
                await mark_answered(row.command_id)
                await save_event(owner_id, msg_id, "vera", answer.raw)
        if held:
            # Строку закроет help_worker, когда задачу возьмут.
            await hold_command(row.command_id, now)
        else:
            await finish_command(row.command_id)
        log.info("voice-worker: поручение %s выполнено", row.command_id)
    except Exception as e:
        status = await fail_command(row.command_id, _reason(e), row.attempts)
        log.warning("voice-worker: поручение %s не выполнено (%s), попытка %d → %s",
                    row.command_id, type(e).__name__, row.attempts, status)
    return True


def _reason(e: Exception) -> str:
    # У BrainError в тексте только HTTP-код — его оставить полезно.
    return f"BrainError {e}" if isinstance(e, BrainError) else type(e).__name__


async def _route_failed(command_id: str) -> tuple[str, str]:
    row = await get_command(command_id)
    if row is None or row.help_state is None:
        return FAILED, ""  # не срочная просьба: как раньше
    return await failure_route(row)


async def _status_notice(send: Send, command_id: str, route: str, agent: str) -> int:
    """Вместо «не смогла»: задача жива. Один раз, квитанция — message_id заявки notice."""
    now = utc_naive_now()
    lost = await claim(command_id, "notice", now, NOTICE_KEY)
    if lost is not None:
        log.info("voice-worker: %s — статус уже в заявке (%s), не повторяю",
                 command_id, lost["state"])
        if lost["state"] != "error":
            await mark_notified(command_id)
        return 0
    row = await get_command(command_id)
    text = {ASSIGNED: assigned_text, OFFERED: offered_text}.get(route, lambda _a: NO_TAKER_TEXT)(agent)
    try:
        if route == ESCALATING:
            await escalate(task_id=row.task_id)
        msg_id = await send(escape(text, quote=False), text)
    except Exception as e:
        state = classify_send_error(e)
        await settle(command_id, "notice", state, key=NOTICE_KEY)
        log.warning("voice-worker: статус по %s не отправлен (%s), %s", command_id,
                    type(e).__name__, "повторю" if state == "error" else "исход неизвестен")
        return 0
    if not msg_id:
        await settle(command_id, "notice", "unknown", key=NOTICE_KEY)
        return 0
    await settle(command_id, "notice", "sent", msg_id, key=NOTICE_KEY)
    if route == ESCALATING and row.help_state == "opened":
        await mark_escalated(command_id, now)
    await mark_notified(command_id)
    return 1


async def notify_failed(send: Send) -> int:
    """Сообщить владельцу о поручениях, выполнить которые не вышло. → сколько.

    Сбой отправки не обрывает проход и не теряет уведомление: без
    `notified_at` поручение попадёт сюда на следующем витке.
    """
    done = 0
    for command_id, instruction, answered in await pending_notifications():
        if answered:
            # Ответ уже ушёл, а упало что-то после (запись события, finish).
            # «Не смогла» поверх полученного ответа — ложь владельцу.
            await mark_notified(command_id)
            continue
        route, agent = await _route_failed(command_id)
        if route == EXECUTOR_FINAL:
            log.info("voice-worker: %s — финал за исполнителем, «не смогла» не шлю", command_id)
            await mark_notified(command_id)
            continue
        if route != FAILED:
            done += await _status_notice(send, command_id, route, agent)
            continue
        text = failed_text(instruction)
        try:
            await send(escape(text, quote=False), text)
        except Exception as e:
            log.warning("voice-worker: не сообщила об ошибке поручения %s (%s) — "
                        "повторю", command_id, type(e).__name__)
            continue
        await mark_notified(command_id)
        done += 1
    return done


async def run_forever(send: Send, owner_id: int) -> None:
    if owner_id == 0:
        # Fail-closed, как `_owner_only` в боте: без адресата поручения ждут
        # в очереди, а не уходят кому попало.
        log.error("voice-worker: OWNER_TELEGRAM_ID не задан — поручения не исполняю")
        return
    log.info("voice-worker: запущен")
    while True:
        try:
            await revive_stale()
            await notify_failed(send)
            await track_help(send, utc_naive_now())
            busy = await process_one(send, owner_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Сбой БД или Telegram не имеет права убить воркер: очередь встала бы молча.
            log.exception("voice-worker: цикл упал")
            busy = False
        if not busy:
            await asyncio.sleep(POLL_S)
