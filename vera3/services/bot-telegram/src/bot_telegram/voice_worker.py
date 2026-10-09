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
from datetime import timedelta
from html import escape

from vera_shared.timeutil import utc_naive_now
from vera_shared.voice_commands import (
    claim_command,
    fail_command,
    finish_command,
    mark_acked,
    mark_answered,
    mark_notified,
    pending_notifications,
    revive_stale,
)
from vera_shared.voice_help.queue_state import mark_opened
from vera_shared.voice_help.room_intake import open_help_task

from bot_telegram.brain import BrainError, ask_brain, save_event
from bot_telegram.formatting import format_reply, plain_fallback
from bot_telegram.help_worker import (
    REPROMPT_TEXT,
    Ask,
    ask_confirmation,
    expire_confirmations,
    opened_ack_text,
    track_help,
)

log = logging.getLogger(__name__)

POLL_S = 2.0
#: `Send(html, plain)` → id отправленного сообщения. plain — запасной текст на
#: случай, если Telegram отклонит HTML (см. formatting.plain_fallback).
Send = Callable[[str, str], Awaitable[int]]


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


async def process_one(send: Send, owner_id: int, ask: Ask | None = None) -> bool:
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
        age = now - row.spoken_at
        if row.answered_at is None and row.acked_at is None and age > MAX_AGE:
            text = stale_text(row.instruction, age)
            await send(escape(text, quote=False), text)
            await mark_answered(row.command_id)
        elif row.kind == "reprompt":
            if row.answered_at is None:
                await send(REPROMPT_TEXT, REPROMPT_TEXT)
                await mark_answered(row.command_id)
        elif row.help_state == "confirm":
            if ask is None:
                raise RuntimeError("confirmation needed but no Ask sender")
            # Без «Да» — ни задачи, ни ответа мозга: строка ждёт в waiting.
            quoted = "quoted" in (row.source or {}).get("doubts", [])
            await ask_confirmation(ask, row.command_id, row.instruction, now,
                                   quoted=quoted)
            return True
        elif row.answered_at is None:
            if row.help_state == "ready":
                task_id = await open_help_task(
                    command_id=row.command_id, event_id=row.event_id,
                    instruction=row.instruction, source=row.source)
                await mark_opened(row.command_id, task_id, now)
            if row.acked_at is None:
                ack = (opened_ack_text(row.instruction)
                       if row.help_state in ("ready", "opened")
                       else ack_text(row.instruction))
                # Поручение — распознанная речь, и «<» в ней сломал бы HTML.
                await send(escape(ack, quote=False), ack)
                await mark_acked(row.command_id)
            answer = await ask_brain(row.instruction, owner_id, owner_id)
            msg_id = await send(
                format_reply(answer.raw, answer.provider, answer.cost_usd,
                             answer.n_results, answer.n_history),
                plain_fallback(answer.raw, answer.provider))
            await mark_answered(row.command_id)
            await save_event(owner_id, msg_id, "vera", answer.raw)
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


async def run_forever(send: Send, owner_id: int, ask: Ask | None = None) -> None:
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
            await expire_confirmations(send, utc_naive_now())
            await track_help(send, utc_naive_now())
            busy = await process_one(send, owner_id, ask)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Сбой БД или Telegram не имеет права убить воркер: очередь встала бы молча.
            log.exception("voice-worker: цикл упал")
            busy = False
        if not busy:
            await asyncio.sleep(POLL_S)
