"""Откуда поллер берёт токен и как сообщает о его смерти.

Порядок: активная строка `slack_auth` (подключение из дашборда), иначе
`SLACK_USER_TOKEN` из окружения. Второй путь оставлен, чтобы уже подключённый
источник не отвалился от появления таблицы.

Отзыв токена гасит строку и пишет причину: без этого дашборд показывал бы
«подключено», пока в логе контейнера каждые 10 минут «нет доступа».
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import select, update
from sqlalchemy.exc import ProgrammingError
from vera_shared.crypto import decrypt
from vera_shared.db.engine import get_session
from vera_shared.db.models_sources import SlackAuthRow
from vera_shared.timeutil import utc_naive_now

log = logging.getLogger("slack")


async def load_token() -> tuple[str, int | None]:
    """→ (токен, id строки slack_auth либо None если токен из окружения)."""
    stored, row_id = "", None
    try:
        async with get_session() as s:
            row = (await s.execute(
                select(SlackAuthRow)
                .where(SlackAuthRow.is_active.is_(True))
                .order_by(SlackAuthRow.id.desc())
            )).scalars().first()
            if row is not None:
                stored, row_id = row.token_enc, row.id
    except ProgrammingError:
        # Деплой привозит код раньше, чем накатывается миграция — окно между
        # ними структурное, оно повторится с каждым новым источником. Внятная
        # строка полезнее трейсбека, и она ничего не скрывает: сказано прямо,
        # что таблицы нет.
        log.error("slack: таблицы slack_auth нет — накати миграцию 025; "
                  "пока беру токен из окружения")
        return os.environ.get("SLACK_USER_TOKEN", ""), None

    if row_id is not None:
        try:
            return decrypt(stored), row_id
        except ValueError as e:
            # Ключа шифрования нет у КОНТЕЙНЕРА — это настройка, а не порча
            # строки. Гасить подключение тут нельзя: дашборд показал бы
            # «отозван», и токен вводили бы заново без толку. Поймано вживую:
            # ingestor-slack поднялся без TOKEN_SECRET и погасил живую строку.
            log.error("slack: нечем расшифровать токен (%s) — проверь TOKEN_SECRET "
                      "у контейнера; строку не трогаю", e)
        except Exception as e:  # noqa: BLE001 — битый шифр не должен ронять контейнер
            log.error("slack: токен в БД не расшифровался (%s) — гашу строку", e)
            await mark_dead(row_id, f"не расшифровался: {e}")

    return os.environ.get("SLACK_USER_TOKEN", ""), None


async def mark_ok(row_id: int | None) -> None:
    if row_id is None:
        return
    async with get_session() as s:
        await s.execute(
            update(SlackAuthRow).where(SlackAuthRow.id == row_id)
            .values(last_ok_at=utc_naive_now(), last_error=None)
        )


async def mark_dead(row_id: int | None, reason: str) -> None:
    """Токен отозван или прав не хватает — гасим строку, чтобы это было видно."""
    if row_id is None:
        return
    async with get_session() as s:
        await s.execute(
            update(SlackAuthRow).where(SlackAuthRow.id == row_id)
            .values(is_active=False, last_error=reason[:500])
        )
