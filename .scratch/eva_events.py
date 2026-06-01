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
        for e in r.scalars():
            meta = e.metadata_ or {}
            sid = meta.get("sender_id")
            uname = (meta.get("sender_username") or "").lower()
            title = (meta.get("chat_title") or "").lower()
            if sid == 876653396 or "eva_alx" in uname or "евочка" in title or "eva_alx" in title:
                print(f"#{e.id} {e.occurred_at}")
                print(f"  meta: chat_id={meta.get('chat_id')} sender={meta.get('sender_username')!r} msg_id={meta.get('message_id')}")
                body = (e.content_text or "").replace("\n", " | ")[:200]
                print(f"  body: {body}")


asyncio.run(main())
