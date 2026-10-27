"""Доставка результата исполнителя срочной задачи владельцу — без миграции.

Хранилище — существующая строка `voice_command_queue`: `help_state` (VARCHAR(16), без
CHECK) несёт этап, `source` (JSONB «откуда») — квитанцию. Этапы:

    taken | opened | … ─намерение─▶ result_pending ─отправка ок─▶ closed (+квитанция)
                                          │ ─Telegram отказал─▶ result_error ─повтор─▶ …
                                          └─ намерение без квитанции на след. проходе
                                             (упали между отправкой и записью)
                                             ─▶ result_unknown (не повторяем, ждёт человека)

Намерение фиксируется отдельной транзакцией ДО отправки; вслепую повторяется только
точный отказ (исключение отправки), неизвестный исход — никогда.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select, update

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskEventRow
from vera_shared.db.models_voice import VoiceCommandRow
from vera_shared.voice_help.delivery_claim import update_source
from vera_shared.voice_help.delivery_claim import update_source

PENDING, ERROR, UNKNOWN = "result_pending", "result_error", "result_unknown"
RESULT_WATCHED = ("taken", PENDING, ERROR)
RESULT_MAX_ERRORS = 5
SINCE_ENV = "VOICE_RESULT_DELIVERY_SINCE"


def parse_since(raw: str) -> datetime | None:
    """ISO (UTC) → naive UTC; пусто или мусор → None (выключено)."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


async def executor_result(room: str, task_id: str) -> tuple[str, str] | None:
    """→ (агент, текст) последнего события done; None, если текста нет."""
    ev = RoomTaskEventRow
    async with get_session() as s:
        row = (await s.execute(
            select(ev.agent, ev.text).where(
                ev.room == room, ev.task_id == task_id, ev.kind == "done")
            .order_by(ev.id.desc()).limit(1))).first()
    if row is None or not (row.text or "").strip():
        return None
    return row.agent, row.text.strip()


async def move(command_id: str, from_states: tuple[str, ...], to: str, now: datetime) -> bool:
    """Условный переход этапа; → True, если именно мы его сделали."""
    async with get_session() as s:
        res = await s.execute(update(VoiceCommandRow)
                              .where(VoiceCommandRow.command_id == command_id,
                                     VoiceCommandRow.help_state.in_(from_states))
                              .values(help_state=to, updated_at=now))
        return res.rowcount == 1


def result_errors(row: VoiceCommandRow) -> int:
    return int((row.source or {}).get("result_errors", 0))


async def record_error(command_id: str, errors: int, now: datetime) -> None:
    await update_source(command_id, {"result_errors": errors}, only_state=PENDING,
                        help_state=ERROR, updated_at=now)


async def record_receipt(command_id: str, message_id: int, now: datetime) -> bool:
    """Квитанция и закрытие одним UPDATE; → True, если записана именно сейчас."""
    return await update_source(
        command_id, {"result_message_id": message_id, "result_sent_at": now.isoformat()},
        only_state=PENDING, help_state="closed", updated_at=now)


def delivery_since() -> datetime | None:
    """Граница включения (деплой ставит время выкатки). Не задана или битая — выключено."""
    return parse_since(os.environ.get(SINCE_ENV, ""))


def log_delivery_mode(log: logging.Logger) -> None:
    raw = os.environ.get(SINCE_ENV, "").strip()
    since = delivery_since()
    if since is not None:
        log.info("voice result delivery: enabled since %s", since.isoformat())
    elif raw:
        log.warning("voice result delivery: disabled (%s invalid: %r)", SINCE_ENV, raw)
    else:
        log.info("voice result delivery: disabled (%s not set)", SINCE_ENV)
