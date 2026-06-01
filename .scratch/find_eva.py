import asyncio
from sqlalchemy import select, desc
from vera_shared.db.engine import get_session
from vera_shared.db.models import Event


async def main() -> None:
    async with get_session() as s:
        r = await s.execute(
            select(Event).where(Event.source == "telegram")
            .order_by(desc(Event.id)).limit(500)
        )
        seen = set()
        for e in r.scalars():
            meta = e.metadata_ or {}
            uname = (meta.get("sender_username") or "").lower()
            title = (meta.get("chat_title") or "").lower()
            sid = meta.get("sender_id")
            for marker in ("eva", "евочка", "ева"):
                if marker in uname or marker in title:
                    key = (uname, sid)
                    if key in seen:
                        break
                    seen.add(key)
                    print(f"sender_username={uname!r} sender_id={sid} chat_title={title!r} chat_id={meta.get('chat_id')}")
                    break


asyncio.run(main())
