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
from html import escape

from vera_shared.voice_commands import (
    claim_command,
    fail_command,
    finish_command,
    mark_acked,
    revive_stale,
)

from bot_telegram.brain import ask_brain, save_event
from bot_telegram.formatting import format_error, format_reply, plain_fallback

log = logging.getLogger(__name__)

POLL_S = 2.0
#: `Send(html, plain)` → id отправленного сообщения. plain — запасной текст на
#: случай, если Telegram отклонит HTML (см. formatting.plain_fallback).
Send = Callable[[str, str], Awaitable[int]]


def ack_text(instruction: str) -> str:
    return f"Услышала: „{instruction}“. Делаю."


async def process_one(send: Send, owner_id: int) -> bool:
    """Ответить на одно поручение. False — очередь пуста."""
    row = await claim_command()
    if row is None:
        return False
    try:
        if row.acked_at is None:
            ack = ack_text(row.instruction)
            # Поручение — распознанная речь, и «<» в ней сломал бы HTML-разметку.
            await send(escape(ack, quote=False), ack)
            await mark_acked(row.command_id)
        answer = await ask_brain(row.instruction, owner_id, owner_id)
        msg_id = await send(
            format_reply(answer.raw, answer.provider, answer.cost_usd,
                         answer.n_results, answer.n_history),
            plain_fallback(answer.raw, answer.provider))
        await save_event(owner_id, msg_id, "vera", answer.raw)
        await finish_command(row.command_id)
        log.info("voice-worker: поручение %s выполнено", row.command_id)
    except Exception as e:
        status = await fail_command(row.command_id, f"{type(e).__name__}: {e}",
                                    row.attempts)
        log.warning("voice-worker: поручение %s не выполнено (%s), попытка %d → %s",
                    row.command_id, type(e).__name__, row.attempts, status)
        if status == "error":
            # Последняя попытка — владелец должен узнать, что ответа не будет.
            text = format_error(e)
            await send(text, text)
    return True


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
            busy = await process_one(send, owner_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Сбой БД или Telegram не имеет права убить воркер: очередь встала бы молча.
            log.exception("voice-worker: цикл упал")
            busy = False
        if not busy:
            await asyncio.sleep(POLL_S)
