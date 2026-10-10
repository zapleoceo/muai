"""Шлюз уведомлений владельцу: единственная точка, через которую бот пишет первым."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from html import escape

from vera_shared.owner_attention import store
from vera_shared.owner_attention.policy import (
    QUIET_FOR,
    WAIT_FOR,
    quiet_enabled,
    timeout_goes_quiet,
)
from vera_shared.timeutil import utc_naive_now

log = logging.getLogger(__name__)

Send = Callable[[str, str], Awaitable[int]]
QUESTION = "question"
DIGEST_HEADER = "Пока было тихо, накопилось:"


async def on_owner_message(now: datetime | None = None, send: Send | None = None) -> None:
    """Сообщение или ответ владельца (Telegram, голос, дашборд) - сразу active.

    Из тишины с `send` (бот) тут же уходит одна сводка; без `send` (дашборд) отложенное
    остаётся в очереди до ближайшей сводки.
    """
    if not quiet_enabled():
        return
    now = now or utc_naive_now()
    state = await store.load_state()
    if state.state == store.ACTIVE:
        return
    if state.state == store.QUIET and send is not None:
        try:
            await _flush(send, now)
        except Exception as e:
            # Сообщение владельца важнее сводки: состояние active, сводку повторит tick.
            log.warning("owner-attention: сводка не ушла (%s), повторит tick", type(e).__name__)
    await store.save_state(store.Attention(), now)


async def _settle(state: store.Attention, now: datetime) -> store.Attention:
    if state.state != store.WAIT10 or state.wait_deadline is None or now < state.wait_deadline:
        return state
    if timeout_goes_quiet(state.wait_deadline):
        settled = store.Attention(store.QUIET, quiet_until=state.wait_deadline + QUIET_FOR)
    else:
        settled = store.Attention()
    await store.save_state(settled, now)
    return settled


def _digest(items: list[store.Deferred]) -> tuple[str, str]:
    lines = [f"- {i.text}" for i in items]
    plain = "\n".join([DIGEST_HEADER, *lines])
    return escape(plain, quote=False), plain


_FLUSH_LOCK = asyncio.Lock()


async def _flush(send: Send, now: datetime) -> None:
    async with _FLUSH_LOCK:
        await _flush_locked(send, now)


async def _flush_locked(send: Send, now: datetime) -> None:
    pending = await store.pending()
    latest: dict[str, store.Deferred] = {}
    for item in pending:
        latest[item.task_id or item.dedup_key] = item
    relevant = [i for i in latest.values() if await store.still_relevant(i)]
    if relevant:
        await send(*_digest(relevant))
    await store.mark_sent([i.dedup_key for i in pending], now)
    await store.save_state(store.Attention(), now)
    log.info("owner-attention: сводка %d из %d отложенных", len(relevant), len(pending))


async def tick(send: Send, now: datetime | None = None) -> store.Attention:
    """Один проход из цикла бота: истечение wait10 и автосводка по quiet_until."""
    if not quiet_enabled():
        return store.Attention()
    now = now or utc_naive_now()
    state = await _settle(await store.load_state(), now)
    if state.state == store.QUIET and state.quiet_until is not None and now >= state.quiet_until:
        await _flush(send, now)
        return store.Attention()
    if state.state == store.ACTIVE and await store.pending():
        await _flush(send, now)
    return state


async def deliver(send: Send, html: str, plain: str, *, kind: str = "status",
                  key: str | None = None, task_id: str | None = None,
                  room: str | None = None, question_id: int | None = None,
                  now: datetime | None = None) -> int | None:
    """Отправить сразу или отложить. -> id сообщения, None - отложено.

    Вопрос (kind="question") запускает wait10 только после успешной отправки;
    статусы и тревоги таймер не трогают.
    """
    if not quiet_enabled():
        return await send(html, plain)
    now = now or utc_naive_now()
    state = await tick(send, now)
    ref = key or f"{kind}:{task_id or plain[:60]}"
    if state.state == store.QUIET:
        await store.defer(store.Deferred(ref[:160], kind, plain, now, room, task_id, question_id))
        return None
    sent = await send(html, plain)
    if kind == QUESTION:
        await store.save_state(store.Attention(
            store.WAIT10, wait_question_ref=ref[:128], wait_deadline=now + WAIT_FOR), now)
    return sent
