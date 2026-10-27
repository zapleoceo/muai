"""Диспетчер офиса: сообщает получателю, что в комнате ждёт задача проекта Vera.

Без LLM и без текста задачи сверх очищенного заголовка. Успешный `room_post` —
НЕ квитанция: он лишь кладёт сообщение в комнату. Квитанция доставки — ответ
получателя на именно это сообщение: `room_post(status="ack",
in_reply_to="dispatch-<task_id>")`. Она отдельна от квитанции исполнения
(`done`/`blocked` по задаче, другое сообщение и статус) и ею не считается.

Шаги: claim от имени `vera` → `room_post` (id `dispatch-<task_id>`, повтор того же
— дубль) → задача остаётся за `vera` в `room_task_wait` до срока (refs:
`dispatched`, `dispatch_attempts`, `dispatch_next_at`). Пришла квитанция — штатная
передача `handoff.offer(vera → получатель)`: аренда `vera` живёт, пока получатель не
сделает `handoff_accept` (ref `handoff_offered`). Предложение не принято к сроку или
отклонено — `handoff.cancel`, задача остаётся за `vera`, повтор с той же паузой
(ref `offer_retry`). Не пришла квитанция к сроку — повтор того же сообщения с
экспоненциальной паузой и джиттером; после `MAX_ATTEMPTS` (общий счётчик доставки и
предложений) — release в `blocked` (задача видна, не потеряна). Задача под аттестацией
передаче не подлежит: для неё прежний release в `open` (её берёт только `dot`). Сбой самого поста считается
попыткой так же. Упал процесс — состояние в refs и в аренде, следующий проход
продолжит без дублей.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import timedelta
from typing import Any

from vera_shared.office.owner_routing import verified_office_origin
from vera_shared.redact import redact_secrets
from vera_shared.room import handoff, task_queue, task_wait, tasks
from vera_shared.room.messages import has_reply, post_message
from vera_shared.timeutil import parse_iso_naive, utc_naive_now
from vera_shared.voice_help.attest_state import is_attested
from vera_shared.voice_help.policy import (
    AGENT,
    DEFAULT_PROJECT,
    ESCALATE_TO,
    RESULT_RECIPIENT_REF,
    attest_recipient_blocked,
    help_room,
)

log = logging.getLogger(__name__)

DISPATCHED_PREFIX = "dispatched:"
ATTEMPTS_PREFIX = "dispatch_attempts:"
NEXT_AT_PREFIX = "dispatch_next_at:"
OWNER_OFFICE_REF = "owner_dispatch: dot"
OFFERED_PREFIX = "handoff_offered:"
OFFER_RETRY_PREFIX = "offer_retry:"
RECIPIENT_PREFIX = RESULT_RECIPIENT_REF.split(":")[0] + ":"
MAX_ATTEMPTS = 3
LEASE_S = 60
RECEIPT_WAIT_S = 600  # срок первой квитанции; дальше удваивается
JITTER = 0.2
TITLE_CHARS = 120
BLOCKED_NOTE = "получатель не подтвердил получение"
OFFER_BLOCKED_NOTE = "получатель не принял передачу задачи"
ATTEST_BLOCKED_NOTE = ("подтверждение через dot разрешает результат только владельцу; "
                       "получатель результата другой")
UNVERIFIED_OFFICE_NOTE = "маршрут Office не подтверждён сохранённым origin"
_NAME = re.compile(r"^[a-z0-9_-]{1,32}$")


def _ref_value(refs: list[dict[str, Any]], prefix: str) -> str | None:
    for r in refs:
        ref = str(r.get("ref", ""))
        if ref.startswith(prefix):
            return ref[len(prefix):].strip()
    return None


def recipient_of(refs: list[dict[str, Any]]) -> str:
    name = (_ref_value(refs, RECIPIENT_PREFIX) or "").lower()
    return name if _NAME.match(name) else ESCALATE_TO


def dispatch_body(task: dict[str, Any]) -> str:
    title = redact_secrets(task.get("title") or "")[:TITLE_CHARS]
    return (f"Задача {task['task_id']} ждёт исполнителя "
            f"(origin: {task['created_by']}, проект {task.get('project')}). {title}").strip()


def _with(refs: list[dict[str, Any]], prefix: str, value: str) -> list[dict[str, Any]]:
    kept = [r for r in refs if not str(r.get("ref", "")).startswith(prefix)]
    return [*kept, {"kind": "source", "ref": f"{prefix} {value}"}]


def _without(refs: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    return [r for r in refs if not str(r.get("ref", "")).startswith(prefix)]


async def _post(task: dict[str, Any]) -> str:
    message_id = f"dispatch-{task['task_id']}"
    await post_message(room=task["room"], message_id=message_id, from_agent=AGENT,
                       to_agent=recipient_of(task["refs"]), task_id=task["task_id"],
                       status="request", body=dispatch_body(task))
    return message_id


def backoff_s(attempt: int) -> int:
    base = RECEIPT_WAIT_S * 2 ** (attempt - 1)
    return int(base * random.uniform(1 - JITTER, 1 + JITTER))


async def _delivered(task: dict[str, Any]) -> bool:
    return await has_reply(room=task["room"], in_reply_to=f"dispatch-{task['task_id']}",
                           from_agent=recipient_of(task["refs"]), status="ack")


async def _attempt(task: dict[str, Any]) -> None:
    """Одна попытка доставки под живой арендой: пост, refs, ожидание квитанции."""
    refs = task["refs"]
    if task["created_by"] == "veranda_bot" and not await verified_office_origin(task):
        await tasks.release(room=task["room"], task_id=task["task_id"], agent=AGENT,
                            fencing_token=task["fencing_token"], status="blocked",
                            note=UNVERIFIED_OFFICE_NOTE)
        log.warning("dispatcher: %s — %s", task["task_id"], UNVERIFIED_OFFICE_NOTE)
        return
    if attest_recipient_blocked(
            refs, _ref_value(refs, RECIPIENT_PREFIX),
            attested=await is_attested(task["room"], task["task_id"])):
        await tasks.release(room=task["room"], task_id=task["task_id"], agent=AGENT,
                            fencing_token=task["fencing_token"], status="blocked",
                            note=ATTEST_BLOCKED_NOTE)
        log.warning("dispatcher: %s — %s", task["task_id"], ATTEST_BLOCKED_NOTE)
        return
    attempts = int(_ref_value(refs, ATTEMPTS_PREFIX) or 0) + 1
    try:
        message_id = await _post(task)
        refs = _with(refs, DISPATCHED_PREFIX, message_id)
    except Exception as e:
        log.warning("dispatcher: %s не доставлена (%s), попытка %d/%d", task["task_id"],
                    type(e).__name__, attempts, MAX_ATTEMPTS)
    pause = backoff_s(attempts)
    refs = _with(refs, ATTEMPTS_PREFIX, str(attempts))
    refs = _with(refs, NEXT_AT_PREFIX, (utc_naive_now() + timedelta(seconds=pause)).isoformat())
    room, task_id, token = task["room"], task["task_id"], task["fencing_token"]
    await tasks.update(room=room, task_id=task_id, agent=AGENT, fencing_token=token,
                       refs=refs, extend_seconds=pause + LEASE_S)
    await task_wait.wait(room=room, task_id=task_id, agent=AGENT, fencing_token=token,
                         until_seconds=pause, reason=f"квитанция доставки, попытка {attempts}")
    log.info("dispatcher: %s → %s, жду квитанцию %d с (попытка %d)", task_id,
             recipient_of(refs), pause, attempts)


async def _offer(task: dict[str, Any]) -> bool:
    """Квитанция есть → штатная передача получателю. Аренда `vera` живёт до приёма."""
    room, task_id, token = task["room"], task["task_id"], task["fencing_token"]
    if await is_attested(room, task_id):  # передача запрещена; берёт только dot
        await tasks.release(room=room, task_id=task_id, agent=AGENT, fencing_token=token,
                            status="open")
        log.info("dispatcher: %s доставлена (аттестована) — отпущена без передачи", task_id)
        return True
    to = recipient_of(task["refs"])
    if task.get("pending_handoff_to") != to:  # уже предложено (рестарт) — второй раз не шлём
        try:
            await handoff.offer(room=room, task_id=task_id, agent=AGENT,
                                fencing_token=token, to_agent=to,
                                note="доставка подтверждена, берите задачу")
        except handoff.HandoffError as e:
            await tasks.release(room=room, task_id=task_id, agent=AGENT, fencing_token=token,
                                status="blocked", note=f"передача невозможна: {e}")
            log.warning("dispatcher: %s — передача невозможна (%s)", task_id, e)
            return True
    pause = RECEIPT_WAIT_S
    refs = _with(_without(task["refs"], OFFER_RETRY_PREFIX), OFFERED_PREFIX, to)
    refs = _with(refs, NEXT_AT_PREFIX, (utc_naive_now() + timedelta(seconds=pause)).isoformat())
    await tasks.update(room=room, task_id=task_id, agent=AGENT, fencing_token=token,
                       refs=refs, extend_seconds=pause + LEASE_S)
    await task_wait.wait(room=room, task_id=task_id, agent=AGENT, fencing_token=token,
                         until_seconds=pause, reason=f"приём передачи агентом {to}")
    log.info("dispatcher: %s предложена %s, жду handoff_accept %d с", task_id, to, pause)
    return True


async def _offer_lapsed(task: dict[str, Any]) -> bool:
    """Предложение отклонено или не принято к сроку: снять и повторить с паузой или блок."""
    room, task_id, token = task["room"], task["task_id"], task["fencing_token"]
    try:
        if task.get("pending_handoff_to"):
            task = await handoff.cancel(room=room, task_id=task_id, agent=AGENT,
                                        fencing_token=token, reason="не принято к сроку")
    except (handoff.HandoffError, tasks.StaleLease) as e:  # accept успел раньше — не трогаем
        log.warning("dispatcher: %s — приём обогнал отмену, оставляю (%s)", task_id, e)
        return True
    token = task["fencing_token"]
    attempts = int(_ref_value(task["refs"], ATTEMPTS_PREFIX) or 0)
    if attempts >= MAX_ATTEMPTS:
        await tasks.release(room=room, task_id=task_id, agent=AGENT, fencing_token=token,
                            status="blocked", note=OFFER_BLOCKED_NOTE)
        log.warning("dispatcher: %s — %s", task_id, OFFER_BLOCKED_NOTE)
        return True
    attempts += 1
    pause = backoff_s(attempts)
    refs = _with(_without(task["refs"], OFFERED_PREFIX), OFFER_RETRY_PREFIX, "1")
    refs = _with(refs, ATTEMPTS_PREFIX, str(attempts))
    refs = _with(refs, NEXT_AT_PREFIX, (utc_naive_now() + timedelta(seconds=pause)).isoformat())
    await tasks.update(room=room, task_id=task_id, agent=AGENT, fencing_token=token,
                       refs=refs, extend_seconds=pause + LEASE_S)
    await task_wait.wait(room=room, task_id=task_id, agent=AGENT, fencing_token=token,
                         until_seconds=pause, reason=f"повтор передачи, попытка {attempts}")
    return True


async def _settle(task: dict[str, Any]) -> bool:
    """Задача уже в ожидании квитанции или приёма. True — что-то сделано."""
    room, task_id, token = task["room"], task["task_id"], task["fencing_token"]
    due = parse_iso_naive(_ref_value(task["refs"], NEXT_AT_PREFIX) or "1970-01-01")
    if _ref_value(task["refs"], OFFERED_PREFIX):
        if task.get("pending_handoff_to") and utc_naive_now() < due:
            return False
        return await _offer_lapsed(task)
    retry_wait = _ref_value(task["refs"], OFFER_RETRY_PREFIX) and utc_naive_now() < due
    if (_ref_value(task["refs"], DISPATCHED_PREFIX) and not retry_wait
            and await _delivered(task)):
        return await _offer(task)
    if utc_naive_now() < due:
        return False
    if int(_ref_value(task["refs"], ATTEMPTS_PREFIX) or 0) >= MAX_ATTEMPTS:
        await tasks.release(room=room, task_id=task_id, agent=AGENT, fencing_token=token,
                            status="blocked", note=BLOCKED_NOTE)
        log.warning("dispatcher: %s — %s", task_id, BLOCKED_NOTE)
        return True
    await _attempt(task)
    return True


async def _pending() -> list[dict[str, Any]]:
    return [t for t in await task_queue.held_by(room=help_room(), agent=AGENT)
            if t["status"] == "in_progress" and _ref_value(t["refs"], NEXT_AT_PREFIX)]


async def _settle_pending() -> bool:
    for pending in await _pending():
        try:
            # claim своего: живая аренда — heartbeat, истёкшая — новый токен
            task = await tasks.claim(room=pending["room"], task_id=pending["task_id"],
                                     agent=AGENT, lease_seconds=LEASE_S)
            if await _settle(task):
                return True
        except (tasks.StaleLease, tasks.TaskBusy) as e:  # в т.ч. отказ ворот B1 (пауза, вопрос)
            log.warning("dispatcher: аренда %s потеряна — пропускаю (%s)", pending["task_id"], e)
    return False


async def dispatch_one() -> bool:
    """Одна единица работы: квитанция/повтор по ожидающей или новая задача. False — пусто."""
    if await _settle_pending():
        return True
    task = await task_queue.next_task(
        room=help_room(), agent=AGENT, project="Veranda", lease_seconds=LEASE_S,
        session=None, account=None, skip_ref_prefix=DISPATCHED_PREFIX,
        required_ref=OWNER_OFFICE_REF, created_by="veranda_bot")
    if task is None:
        task = await task_queue.next_task(
            room=help_room(), agent=AGENT, project=DEFAULT_PROJECT, lease_seconds=LEASE_S,
            session=None, account=None, skip_ref_prefix=DISPATCHED_PREFIX)
    if task is None:
        return False
    try:
        await _attempt(task)
    except tasks.StaleLease:
        log.warning("dispatcher: аренда %s потеряна — пропускаю", task["task_id"])
    return True


async def run_forever(owner_id: int) -> None:
    if owner_id == 0:
        log.error("dispatcher: OWNER_TELEGRAM_ID is missing; dispatch disabled")
        return
    while True:
        try:
            await dispatch_one()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("dispatcher: pass failed")
        await asyncio.sleep(2.0)
