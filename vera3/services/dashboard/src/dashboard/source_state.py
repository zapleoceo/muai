"""Подключён источник или нет — один ответ на источник, из его таблицы.

Раньше подпись кнопки выбиралась по числу событий. Это врало дважды: Instagram
с 353 событиями и мёртвой сессией предлагал «Переподключить», как будто всё в
порядке, а свежеподключённый Slack — «Подключить», как будто ещё нет. Число
событий говорит про историю, а не про доступ.

Состояние берётся оттуда, где оно на самом деле лежит: `gmail_accounts`,
`telegram_sessions`, `instagram_sessions`, `slack_auth`, `trello_boards`.

`connected=None` — у источника нет понятия подключения (внутренние, разовый
импорт). Кнопки у него тоже нет.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import func, select, update
from vera_shared.db.engine import get_session
from vera_shared.db.models_sources import (
    GmailAccountRow,
    InstagramSessionRow,
    SlackAuthRow,
    TelegramSessionRow,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class State:
    connected: bool | None
    #: что именно подключено (или почему нет) — одной строкой, для списка
    label: str = ""
    #: что произойдёт при отключении — показывается на подтверждении
    affects: str = ""


UNKNOWN = State(connected=None)


async def _counts(model, active_col) -> tuple[int, int]:
    async with get_session() as s:
        total = (await s.execute(select(func.count()).select_from(model))).scalar_one()
        live = (await s.execute(
            select(func.count()).select_from(model).where(active_col.is_(True))
        )).scalar_one()
    return total, live


async def _gmail() -> State:
    async with get_session() as s:
        rows = (await s.execute(select(GmailAccountRow))).scalars().all()
    live = [r for r in rows if r.is_active and not r.needs_reauth]
    dead = [r for r in rows if not r.is_active or r.needs_reauth]
    if not rows:
        return State(False, "ящиков нет")
    if not live:
        return State(False, f"все {len(rows)} ящика отвалились")
    label = f"{len(live)} из {len(rows)} ящиков"
    if dead:
        label += f" · {len(dead)} отвалился"
    return State(True, label, f"{len(live)} ящиков перестанут опрашиваться")


async def _telegram() -> State:
    total, live = await _counts(TelegramSessionRow, TelegramSessionRow.is_active)
    if not total:
        return State(False, "сессии нет")
    if not live:
        return State(False, "сессия неактивна")
    return State(True, "userbot активен", "поток сообщений остановится")


async def _instagram() -> State:
    async with get_session() as s:
        rows = (await s.execute(select(InstagramSessionRow))).scalars().all()
    live = [r for r in rows if r.is_active]
    if not rows:
        return State(False, "сессии нет")
    if not live:
        return State(False, "сессия неактивна — нужен повторный вход")
    return State(True, f"@{live[0].username}", "опрос личных сообщений остановится")


async def _slack() -> State:
    async with get_session() as s:
        rows = (await s.execute(select(SlackAuthRow))).scalars().all()
    live = [r for r in rows if r.is_active]
    if not rows:
        return State(False, "токена нет")
    if not live:
        reason = (rows[0].last_error or "токен отозван")[:60]
        return State(False, reason)
    row = live[0]
    return State(True, f"{row.team_name or row.team_id} · {row.username}",
                 "опрос каналов и тредов остановится")


async def _trello() -> State:
    """У Trello секрета в БД нет — ключ в infra/.env, отключать из UI нечего.
    Судим по тому, добрался ли опрос хоть до одной доски."""
    from vera_shared.db.models_sources import TrelloBoardRow
    async with get_session() as s:
        rows = (await s.execute(select(TrelloBoardRow))).scalars().all()
    live = [r for r in rows if r.is_active]
    if not rows:
        return State(False, "ключ не задан — досок не видно")
    return State(True, f"{len(live)} досок")


PROVIDERS: dict[str, Callable[[], Awaitable[State]]] = {
    "gmail": _gmail,
    "telegram": _telegram,
    "instagram": _instagram,
    "slack": _slack,
    "trello": _trello,
}


async def state_of(key: str) -> State:
    """Состояние одного источника. Сбой одного НЕ роняет страницу.

    Поймано вживую: `trello_boards` не была накатана на прод, и запрос к ней
    уронил всю страницу из четырнадцати источников пятисоткой. Страница-список
    обязана переживать сломанный источник — иначе один ненакатанный migration
    прячет состояние всех остальных.
    """
    provider = PROVIDERS.get(key)
    if provider is None:
        return UNKNOWN
    try:
        return await provider()
    except Exception as e:  # noqa: BLE001 — один источник не роняет список
        log.warning("состояние источника %s не прочитал: %s", key, e)
        return State(False, _why(e))


def _why(error: Exception) -> str:
    """Причина коротко и по делу, а не «внутренняя ошибка»."""
    text = str(error)
    if "does not exist" in text or "no such table" in text:
        return "таблица не создана — миграция не накатана"
    return f"состояние не прочитано: {type(error).__name__}"


# ─── отключение ─────────────────────────────────────────────────────────────
# Гасим строку, а не удаляем секрет: отключение обратимо, а «удалить» — нет.
# Событий это не касается — отключение останавливает приём, а не стирает память.

_DEACTIVATE: dict[str, tuple] = {
    "gmail": (GmailAccountRow, GmailAccountRow.is_active),
    "telegram": (TelegramSessionRow, TelegramSessionRow.is_active),
    "instagram": (InstagramSessionRow, InstagramSessionRow.is_active),
    "slack": (SlackAuthRow, SlackAuthRow.is_active),
}


def can_disconnect(key: str) -> bool:
    return key in _DEACTIVATE


async def disconnect(key: str) -> int:
    """Погасить все активные строки источника. → сколько погасили."""
    entry = _DEACTIVATE.get(key)
    if entry is None:
        return 0
    model, active_col = entry
    async with get_session() as s:
        result = await s.execute(
            update(model).where(active_col.is_(True)).values(is_active=False)
        )
    return result.rowcount or 0
