"""Read-only inventory of every chat Vera knows, plus an events summary.

Run on a trusted host (laptop or prod) where the SQLite DB is reachable:

    DB_PATH=/var/www/vera/data/vera.db PYTHONPATH=shared python scripts/list_chats.py

Inside the vera-core container DB_PATH defaults to /data/vera.db, so:

    docker compose exec -T vera-core sh -c \
      'PYTHONPATH=/app/shared python /app/scripts/list_chats.py'

SELECT-only. It never writes, so it is safe against the live DB.
"""

import asyncio

from sqlalchemy import func, select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Event, TgDialog


async def _chats() -> list[TgDialog]:
    async with get_session() as session:
        result = await session.execute(
            select(TgDialog).order_by(TgDialog.last_message_date.desc().nullslast())
        )
        return list(result.scalars().all())


async def _event_counts() -> list[tuple[str, int]]:
    async with get_session() as session:
        result = await session.execute(
            select(Event.source, func.count())
            .group_by(Event.source)
            .order_by(func.count().desc())
        )
        return [(src, n) for src, n in result.all()]


def _print_chats(chats: list[TgDialog]) -> None:
    print(f"\n=== Telegram chats (tg_dialogs): {len(chats)} ===")
    print(f"{'id':>14}  {'type':<11}  {'unread':>6}  {'last_message':<19}  name")
    for c in chats:
        last = c.last_message_date.strftime("%Y-%m-%d %H:%M") if c.last_message_date else "-"
        handle = f"@{c.username}" if c.username else ""
        print(f"{c.id:>14}  {c.type:<11}  {c.unread_count:>6}  {last:<19}  {c.name} {handle}".rstrip())

    by_type: dict[str, int] = {}
    for c in chats:
        by_type[c.type] = by_type.get(c.type, 0) + 1
    summary = ", ".join(f"{t}: {n}" for t, n in sorted(by_type.items()))
    print(f"\nby type → {summary or '(none)'}")


def _print_events(counts: list[tuple[str, int]]) -> None:
    total = sum(n for _, n in counts)
    print(f"\n=== Events by source: {total} total ===")
    for source, n in counts:
        print(f"  {source:<14} {n}")


async def main() -> None:
    chats = await _chats()
    counts = await _event_counts()
    _print_chats(chats)
    _print_events(counts)


if __name__ == "__main__":
    asyncio.run(main())
