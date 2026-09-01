#!/usr/bin/env python
"""Держит очередь распознавания медиа заполненной — по той же политике, что загрузка.

Заменил `vera-media-requeue.sh`, где условия отбора были копией денилиста
названий чатов прямо в SQL. Одна политика (`vera_shared.media_policy`) на
загрузку, доливку и уборку — расходиться нечему.

Три шага за прогон:

1. **Уборка.** Из `media_pending` уходит всё, что сегодняшняя политика не
   пропускает: фото вещательных каналов, стикеры, группы без участия
   владельца. Событие остаётся в мозге с заглушкой `[photo]`, только перестаёт
   занимать место в дефицитной очереди. Причина пишется в `media_skip_reason`.
2. **Пересмотр.** Пропущенное раньше по «нет участия» возвращается, если
   владелец в этом чате уже пишет: решение принимается по данным, а данные
   меняются.
3. **Доливка** до `VERA_MEDIA_QUEUE_TARGET` из восстановимых провалов —
   голосовые вперёд, дальше свежие фото, и только то, что политика пропускает.

Запуск (крон раз в 3 часа):
    docker exec vera3-gateway python /app/scripts/media_requeue.py
    python scripts/media_requeue.py --dry-run   # показать, ничего не меняя
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os

from sqlalchemy import select, text
from vera_shared.chat_activity import min_own_messages, own_message_count
from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models import EventRow
from vera_shared.media_policy import SKIP_NO_PARTICIPATION, media_skip_reason

log = logging.getLogger("media-requeue")

TARGET = int(os.environ.get("VERA_MEDIA_QUEUE_TARGET", "800"))

#: Провал, после которого повторять бессмысленно: сообщение или файл больше не
#: достать. Остальные провалы — транспортные, их и доливаем.
PERMANENT = (
    "%Could not find the input entity%",
    "%message not found%",
    "%too large%",
    "%413%",
)


async def _rows(sql: str, **params) -> list[dict]:
    async with get_session() as s:
        return [dict(r) for r in (await s.execute(text(sql), params)).mappings().all()]


async def _retag(ids: list[int], *, status: str, drop_keys: tuple[str, ...] = (),
                 set_meta: dict | None = None) -> int:
    """Сменить статус и метаданные пачке событий.

    Через ORM, а не UPDATE с `jsonb_build_object` и `id = ANY(...)`: те есть
    только в Postgres, а очередь и её уборка обязаны проверяться тестами на
    SQLite. Объёмы маленькие (очередь ≤ 800), так что цена нулевая.
    """
    if not ids:
        return 0
    async with get_session() as s:
        rows = (await s.execute(
            select(EventRow).where(EventRow.id.in_(ids)))).scalars().all()
        for row in rows:
            meta = dict(row.metadata_ or {})
            for key in drop_keys:
                meta.pop(key, None)
            meta.update(set_meta or {})
            row.metadata_ = meta          # присваиваем новый dict: мутацию ORM не видит
            row.triage_status = status
            row.triage_error = None
        return len(rows)


async def _verdicts(rows: list[dict], min_own: int) -> dict[int, str | None]:
    """id события → причина пропуска (None = распознаём). Кэш на чат внутри."""
    out: dict[int, str | None] = {}
    for row in rows:
        own = await own_message_count(row["chat_id"])
        out[row["id"]] = media_skip_reason(
            row["media_kind"], row["chat_kind"],
            own_messages=own, min_own_messages=min_own)
    return out


async def sweep(min_own: int, dry_run: bool) -> dict[str, int]:
    """Шаг 1: выкинуть из очереди то, что политика не пропускает."""
    rows = await _rows("""
        SELECT id,
               CAST(metadata->>'chat_id' AS TEXT) AS chat_id,
               metadata->>'chat_kind'  AS chat_kind,
               metadata->>'media_kind' AS media_kind
        FROM events WHERE triage_status = 'media_pending'
    """)
    verdicts = await _verdicts(rows, min_own)
    drop = {eid: reason for eid, reason in verdicts.items() if reason is not None}
    counts: dict[str, int] = {}
    for reason in drop.values():
        counts[reason] = counts.get(reason, 0) + 1
    if drop and not dry_run:
        for reason in counts:
            await _retag([eid for eid, r in drop.items() if r == reason],
                         status="pending",
                         set_meta={"media_skip_reason": reason,
                                   "needs_recognition": False})
    return counts


async def revisit(min_own: int, dry_run: bool) -> int:
    """Шаг 2: вернуть пропущенное по «нет участия», если участие появилось."""
    rows = await _rows("""
        SELECT id,
               CAST(metadata->>'chat_id' AS TEXT) AS chat_id,
               metadata->>'chat_kind'  AS chat_kind,
               metadata->>'media_kind' AS media_kind
        FROM events
        WHERE triage_status = 'done'
          AND metadata->>'media_skip_reason' = :reason
    """, reason=SKIP_NO_PARTICIPATION)
    verdicts = await _verdicts(rows, min_own)
    back = [eid for eid, reason in verdicts.items() if reason is None]
    if back and not dry_run:
        await _retag(back, status="media_pending",
                     drop_keys=("media_skip_reason",),
                     set_meta={"needs_recognition": True})
    return len(back)


async def top_up(min_own: int, dry_run: bool) -> tuple[int, int, int]:
    """Шаг 3: долить очередь из восстановимых провалов. (было, добавлено, остаток)."""
    pending = (await _rows(
        "SELECT COUNT(*) AS n FROM events WHERE triage_status='media_pending'"
    ))[0]["n"]
    need = TARGET - pending
    if need <= 0:
        return pending, 0, 0

    not_permanent = " ".join(
        f"AND COALESCE(triage_error,'') NOT LIKE '{p}'" for p in PERMANENT)
    # Берём с запасом: часть кандидатов политика отсеет, и добор одним
    # запросом дешевле, чем цикл «выбрал — проверил — не хватило».
    candidates = await _rows(f"""
        SELECT id,
               CAST(metadata->>'chat_id' AS TEXT) AS chat_id,
               metadata->>'chat_kind'  AS chat_kind,
               metadata->>'media_kind' AS media_kind
        FROM events
        WHERE metadata->>'media_recognition' = 'failed'
          AND triage_status = 'done'
          {not_permanent}
        ORDER BY (metadata->>'media_kind' IN ('voice','audio')) DESC,
                 occurred_at DESC
        LIMIT :lim
    """, lim=need * 4)
    verdicts = await _verdicts(candidates, min_own)
    # Порядок из SQL сохраняем: голосовые вперёд, дальше свежие.
    take = [row["id"] for row in candidates if verdicts[row["id"]] is None][:need]
    if take and not dry_run:
        await _retag(take, status="media_pending",
                     drop_keys=("media_recognition", "media_retry_count",
                                "media_next_retry_at", "media_skip_reason"),
                     set_meta={"needs_recognition": True})
    rejected = len(candidates) - len([1 for v in verdicts.values() if v is None])
    return pending, len(take), rejected


async def main(dry_run: bool) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    await init_engine()
    min_own = await min_own_messages()

    dropped = await sweep(min_own, dry_run)
    returned = await revisit(min_own, dry_run)
    was, added, rejected = await top_up(min_own, dry_run)

    prefix = "[dry-run] " if dry_run else ""
    log.info("%sочередь была %d, порог участия %d", prefix, was, min_own)
    if dropped:
        log.info("%sвыкинуто как непроходящее политику: %s", prefix,
                 ", ".join(f"{r}={n}" for r, n in sorted(dropped.items())))
    if returned:
        log.info("%sвозвращено (участие появилось): %d", prefix, returned)
    log.info("%sдолито %d, из кандидатов отсеяно политикой %d", prefix, added, rejected)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="показать, что сделал бы; ничего не менять")
    asyncio.run(main(ap.parse_args().dry_run))
