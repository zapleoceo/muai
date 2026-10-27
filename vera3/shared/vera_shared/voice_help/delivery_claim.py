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

from sqlalchemy import update

from vera_shared.db.engine import get_session
from vera_shared.db.models_voice import VoiceCommandRow

KEY = "delivery"
CAS_TRIES = 8


async def _swap(command_id: str, mutate: Any) -> dict[str, Any] | None:
    """mutate(delivery|None) → новый delivery или None (отказ). → итоговый delivery."""
    for _ in range(CAS_TRIES):
        async with get_session() as s:
            row = await s.get(VoiceCommandRow, command_id)
            if row is None:
                return None
            old = row.source
            current = (old or {}).get(KEY)
        new = mutate(current)
        if new is None:
            return None
        guard = (VoiceCommandRow.source.is_(None) if old is None
                 else VoiceCommandRow.source == old)
        async with get_session() as s:
            res = await s.execute(update(VoiceCommandRow)
                                  .where(VoiceCommandRow.command_id == command_id, guard)
                                  .values(source={**(old or {}), KEY: new}))
            if res.rowcount == 1:
                return new
    return None


async def claim(command_id: str, owner: str, now: datetime) -> dict[str, Any] | None:
    """→ None, если заявку взяли мы (отправляем); иначе текущая заявка (не отправляем)."""
    seen: dict[str, Any] = {}

    def take(current: dict[str, Any] | None) -> dict[str, Any] | None:
        if current is not None and not (current["owner"] == owner and current["state"] == "error"):
            seen.update(current)
            return None
        return {"owner": owner, "state": "claimed", "at": now.isoformat()}

    won = await _swap(command_id, take)
    return None if won is not None else (seen or {"owner": "?", "state": "unknown"})


async def settle(command_id: str, owner: str, state: str, message_id: int | None = None) -> None:
    def put(current: dict[str, Any] | None) -> dict[str, Any] | None:
        if current is None or current["owner"] != owner:
            return None
        return {**current, "state": state,
                **({"message_id": message_id} if message_id else {})}

    await _swap(command_id, put)


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
