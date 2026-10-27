"""Единая заявка на сообщение владельцу по команде: ответ мозга ИЛИ результат исполнителя.

Заявка — ключ `delivery` в `voice_command_queue.source` (JSONB, без миграции), берётся
ДО отправки compare-and-swap'ом: UPDATE проходит, только если `source` не менялся с
момента чтения. Кто взял — отправляет; проигравший не отправляет. Состояния:

    claimed ──отправка ок──▶ sent (message_id)
       │ ─Telegram отказал─▶ error  (владелец заявки может взять её снова)
       └ упали после отправки ─ остаётся claimed = исход неизвестен, автоповтора нет

Ответ мозга тоже считается доставкой: после него результат исполнителя не шлётся.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from vera_shared.db.engine import get_session
from vera_shared.db.models_voice import VoiceCommandRow

KEY = "delivery"
CAS_TRIES = 8


async def _swap(command_id: str, mutate: Any, key: str = KEY) -> dict[str, Any] | None:
    """mutate(delivery|None) → новый delivery или None (отказ). → итоговый delivery."""
    for _ in range(CAS_TRIES):
        async with get_session() as s:
            row = await s.get(VoiceCommandRow, command_id)
            if row is None:
                return None
            old = row.source
            current = (old or {}).get(key)
        new = mutate(current)
        if new is None:
            return None
        guard = (VoiceCommandRow.source.is_(None) if old is None
                 else VoiceCommandRow.source == old)
        async with get_session() as s:
            res = await s.execute(update(VoiceCommandRow)
                                  .where(VoiceCommandRow.command_id == command_id, guard)
                                  .values(source={**(old or {}), key: new}))
            if res.rowcount == 1:
                return new
    return None


async def claim(command_id: str, owner: str, now: datetime,
                key: str = KEY) -> dict[str, Any] | None:
    """→ None, если заявку взяли мы (отправляем); иначе текущая заявка (не отправляем)."""
    seen: dict[str, Any] = {}

    def take(current: dict[str, Any] | None) -> dict[str, Any] | None:
        if current is not None and current["state"] != "error":
            seen.update(current)
            return None
        return {"owner": owner, "state": "claimed", "at": now.isoformat()}

    won = await _swap(command_id, take, key)
    return None if won is not None else (seen or {"owner": "?", "state": "unknown"})


async def settle(command_id: str, owner: str, state: str, message_id: int | None = None,
                 key: str = KEY) -> None:
    def put(current: dict[str, Any] | None) -> dict[str, Any] | None:
        if current is None or current["owner"] != owner:
            return None
        return {**current, "state": state,
                **({"message_id": message_id} if message_id else {})}

    await _swap(command_id, put, key)


async def update_source(command_id: str, extra: dict[str, Any], *, only_state: str,
                        **values: Any) -> bool:
    """CAS-слияние в `source` вместе с другими колонками, если help_state == only_state.

    Тот же защитный шаг, что у заявки: параллельная запись `delivery` не затирается.
    """
    for _ in range(CAS_TRIES):
        async with get_session() as s:
            row = await s.get(VoiceCommandRow, command_id)
            if row is None or row.help_state != only_state:
                return False
            old = row.source
        guard = (VoiceCommandRow.source.is_(None) if old is None
                 else VoiceCommandRow.source == old)
        async with get_session() as s:
            res = await s.execute(update(VoiceCommandRow).where(
                VoiceCommandRow.command_id == command_id,
                VoiceCommandRow.help_state == only_state, guard)
                .values(source={**(old or {}), **extra}, **values))
            if res.rowcount == 1:
                return True
    return False


DEFINITE_FAILURES = frozenset({
    "TelegramBadRequest", "TelegramForbiddenError", "TelegramUnauthorized",
    "TelegramNotFound", "TelegramRetryAfter", "TelegramConflictError",
    "TelegramEntityTooLarge", "TelegramMigrateToChat",
})


def classify_send_error(exc: BaseException) -> str:
    """`error` — Telegram точно не доставил (безопасно повторить); иначе `unknown`.

    Таймаут после отправки запроса, 5xx, обрыв соединения — исход неизвестен:
    сообщение могло дойти, автоповтора нет.
    """
    names = {c.__name__ for c in type(exc).__mro__}
    if names & DEFINITE_FAILURES or isinstance(exc, ConnectionRefusedError):
        return "error"
    if isinstance(exc, RuntimeError) and "OWNER_TELEGRAM_ID not set" in str(exc):
        return "error"  # отказ до обращения к Telegram
    return "unknown"


async def claim_unless_accepted(command_id: str, room: str, task_id: str, owner: str,
                                now: datetime) -> tuple[str, Any]:
    """Заявка на финал мозга, пока исполнитель не принял задачу — одной транзакцией.

    Блокируем строку задачи (её же блокирует `handoff.accept`) и строку очереди,
    перепроверяем держателя и заявку, и только потом берём заявку. → ("won", None) |
    ("executor", агент) — приняли до нас | ("lost", заявка) — финал уже занят.
    """
    from vera_shared.db.models_room import RoomTaskEventRow, RoomTaskRow
    from vera_shared.voice_help.tracking import holder_of

    async with get_session() as s:
        task = await s.get(RoomTaskRow, (room, task_id), with_for_update=True)
        holder, _ = await holder_of(s, room, task_id, task)
        if holder:
            return "executor", holder
        # держатель исчез (done/release), но задачу вёл исполнитель: финал его
        accepted = (await s.execute(
            select(RoomTaskEventRow.agent).where(
                RoomTaskEventRow.room == room, RoomTaskEventRow.task_id == task_id,
                RoomTaskEventRow.kind == "handoff_accept")
            .order_by(RoomTaskEventRow.id.desc()).limit(1))).scalar_one_or_none()
        if accepted or (task is not None and task.status == "done"):
            return "executor", accepted or "исполнитель"
        row = await s.get(VoiceCommandRow, command_id, with_for_update=True)
        if row is None:
            return "lost", {"owner": "?", "state": "unknown"}
        current = (row.source or {}).get(KEY)
        if current is not None and current["state"] != "error":
            return "lost", current
        row.source = {**(row.source or {}),
                      KEY: {"owner": owner, "state": "claimed", "at": now.isoformat()}}
        return "won", None
