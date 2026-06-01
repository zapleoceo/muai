import asyncio
from datetime import datetime, timedelta
from sqlalchemy import select, desc
from vera_shared.db.engine import get_session
from vera_shared.db.models import Event


async def main() -> None:
    cutoff = datetime.utcnow() - timedelta(days=10)
    async with get_session() as s:
        r = await s.execute(
            select(Event).where(Event.source == "telegram")
            .order_by(desc(Event.id)).limit(5000)
        )
        rows = []
        for e in r.scalars():
            meta = e.metadata_ or {}
            if meta.get("chat_id") == 876653396 and e.occurred_at >= cutoff:
                msg = (e.content_text or "").split("\n---\n", 1)[-1].strip()
                rows.append((e.occurred_at, msg))
        rows.sort()
    print(f"=== {len(rows)} сообщений от Eva за 10 дней ===\n")
    for ts, msg in rows:
        print(f"[{ts.strftime('%m-%d %H:%M')}] {msg}")


asyncio.run(main())
