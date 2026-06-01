import asyncio
from sqlalchemy import select, desc
from vera_shared.db.engine import get_session
from vera_shared.db.models import Event


async def main() -> None:
    async with get_session() as s:
        r = await s.execute(
            select(Event).where(Event.source == "telegram")
            .order_by(desc(Event.id)).limit(2000)
        )
        rows = []
        for e in r.scalars():
            meta = e.metadata_ or {}
            if meta.get("chat_id") == 876653396:
                # Extract the actual message after the '---' header
                txt = e.content_text or ""
                msg = txt.split("\n---\n", 1)[-1].strip()
                rows.append((e.occurred_at, meta.get("sender_username"), msg))
        rows.sort()  # oldest first
    for ts, sender, msg in rows[-50:]:
        print(f"[{ts.strftime('%m-%d %H:%M')}] {sender}: {msg[:400]}")
        print("---")


asyncio.run(main())
