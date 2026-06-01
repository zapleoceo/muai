import asyncio
from datetime import date
from sqlalchemy import select, desc
from vera_shared.db.engine import get_session
from vera_shared.db.models import Event


async def main() -> None:
    today = date.today()
    async with get_session() as s:
        r = await s.execute(
            select(Event).where(Event.source == "telegram")
            .order_by(desc(Event.id)).limit(2000)
        )
        rows = []
        for e in r.scalars():
            meta = e.metadata_ or {}
            if meta.get("chat_id") == 876653396 and e.occurred_at.date() == today:
                msg = (e.content_text or "").split("\n---\n", 1)[-1].strip()
                rows.append((e.occurred_at, msg))
        rows.sort()
    for ts, msg in rows:
        print(f"{ts.strftime('%H:%M')}  {msg}")


asyncio.run(main())
